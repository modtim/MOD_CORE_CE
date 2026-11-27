"""
Job Scheduler.

Uses APScheduler to run periodic maintenance jobs inside the API process.
Each job is a simple function that gets a DB session and does its work.
"""

import importlib.util
import json
from product_names import themed_logger
import os
import functools
from datetime import datetime, timedelta, timezone
from utils.paid_import import classify_missing_import, is_paid_tree, optional_paid
from utils.registries import tenant_registry
import models

logger = themed_logger(__name__, 'scheduler')

# GS.PSR.8 — calibration staleness floor registered here (the module
# that owns the calibration-staleness sweep) so it is available at
# import time; readers in this file resolve it at call time.
from services import platform_settings as _psr  # noqa: E402

# ---------- CE paid-module guard (1647a) ----------
# Module-level dedup set: (job_id_or_qualname, missing_module) -> None.
# A CE skip logs ONCE per process per (job, module) pair, not every tick.
_CE_SEEN: set[tuple[str, str]] = set()

# Guard: wrap every job callable.  Callable from tests directly.
def _ce_guard_job(func, job_id: str):  # type: ignore[no-untyped-def]
    """Wrap *func* so ImportError on a missing first-party module is
    caught, deduped, logged once, and returns None; other errors re-raise.
    Returns func unchanged if it isn't callable (string refs like
    "module:func").
    """
    if not callable(func):
        return func

    _capture = func  # snapshot before reassign

    @functools.wraps(_capture)
    def _guarded(*a, **kw):  # type: ignore[no-untyped-bc]
        try:
            return _capture(*a, **kw)
        except ImportError as exc:
            missing = classify_missing_import(exc)
            if missing is not None:
                key = (job_id, missing)
                if key not in _CE_SEEN:
                    _CE_SEEN.add(key)
                    logger.info(
                        "Scheduler job %s skipped: %s not in this edition",
                        job_id, missing,
                    )
                return None
            raise

    return _guarded

_scheduler = None
_watchdog_started = False

_psr.register(
    "calibration.staleness_floor_days",
    int,
    30,
    env_fallback="CALIBRATION_STALENESS_FLOOR_DAYS",
    group="calibration",
    description="Lower bound (days) on a hardware profile's effective "
                "calibration-staleness threshold — the package-delta "
                "ratchet can shorten the ceiling down to this floor, "
                "never below it (default 30, mirrors the legacy "
                "CALIBRATION_STALENESS_FLOOR_DAYS).",
)

# sz-payg.10 (payg-billing-hardening §3.2) — admin kill-switch for the
# hourly meter-sampling sweep (utils/meter_sampler.sample_all). Default ON;
# flipping it off is a no-restart way to pause sampling without touching
# the cron registration itself.
_psr.register(
    "billing.meter_sampling_enabled",
    bool,
    True,
    group="billing",
    description="Run the hourly (:05) meter-sampling sweep that writes "
                "BillingMeterSample rows for the storage/concurrent state "
                "meters (default ON).",
)

# BUG.B85b — orphan-run finalize branch of the zombie reconcile (see
# _run_zombie_run_reconcile): runs in RUNNING/QUEUED with ZERO NodeRuns and
# ZERO ApiTaskQueue rows that every started_at-keyed rescuer is blind to.
_psr.register(
    "orchestration.orphan_run_fail_enabled",
    bool,
    True,
    group="orchestration",
    description="Finalize never-dispatched orphan runs (zero NodeRuns, zero "
                "tasks, RUNNING/QUEUED) as FAILED instead of leaving them "
                "stranded forever (default ON).",
)
_psr.register(
    "orchestration.orphan_run_fail_minutes",
    int,
    45,
    group="orchestration",
    description="Age window (minutes) past which an orphan run (zero NodeRuns, "
                "zero tasks) is finalized FAILED by the zombie reconcile "
                "(default 45).",
)

# BUG.B85e — "run 2038" shape: NodeRuns EXIST (so the zero-NodeRun orphan
# branch above never sees them) but are ALL stuck QUEUED and there is not,
# and never was, a single ApiTaskQueue row for the run — nothing will ever
# dispatch them. Separate toggle/grace from orphan_run_fail_* above because
# the failure shape (never-enqueued NodeRuns) is distinct from the
# zero-NodeRun orphan case.
_psr.register(
    "orchestration.queued_never_enqueued_fail_enabled",
    bool,
    True,
    group="orchestration",
    description="Finalize runs whose NodeRuns are all stuck QUEUED with zero "
                "ApiTaskQueue rows ever created for the run (never enqueued) "
                "as FAILED instead of leaving them stranded forever (default "
                "ON).",
)
_psr.register(
    "orchestration.queued_never_enqueued_fail_minutes",
    int,
    45,
    group="orchestration",
    description="Age window (minutes) past which a run whose NodeRuns are all "
                "stuck QUEUED with zero ApiTaskQueue rows ever created is "
                "finalized FAILED by the zombie reconcile (default 45).",
)

# scheduler-drift-recal-scan (A5): admin-toggleable; the job is always
# registered and the tick no-ops while disabled, so a flip needs no restart.
_psr.register(
    "scheduler.drift_scan_enabled",
    bool,
    False,
    env_fallback="SCHEDULER_DRIFT_SCAN_ENABLED",
    group="scheduler",
    description="Daily per-(node, hardware) drift scan that queues re-"
                "calibration for drifting pairs. Queues REAL calibration "
                "tasks (default OFF).",
)
_psr.register(
    "scheduler.drift_scan_cron",
    str,
    "0 5 * * *",
    env_fallback="SCHEDULER_DRIFT_SCAN_CRON",
    group="scheduler",
    description="Crontab (UTC) for the drift scan; read at API start "
                "(default '0 5 * * *').",
)
# BUG.B85 zombie-RUNNING run reconcile: admin-toggleable (default ON = live,
# BUG.B85 run 2038 shipped dark for days with nothing finalizing it); read at
# call time, so a flip needs no restart. STACK_ZOMBIE_RUN_RECONCILE_ENABLED
# is a deprecated env fallback, only consulted when no DB row exists yet.
_psr.register(
    "scheduler.zombie_run_reconcile_enabled",
    bool,
    True,
    env_fallback="STACK_ZOMBIE_RUN_RECONCILE_ENABLED",
    group="scheduler",
    description="Finalize workflow runs stuck RUNNING/QUEUED with no "
                "in-flight work (status + audit only, default ON). OFF = "
                "dry-run log of what it would finalize.",
)
_psr.register(
    "scheduler.zombie_run_reconcile_minutes",
    int,
    10,
    env_fallback="STACK_ZOMBIE_RUN_RECONCILE_MINUTES",
    group="scheduler",
    description="Interval (minutes) of the zombie-run reconcile sweep; read "
                "at API start (default 10).",
)
_psr.register(
    "usage.concurrent_processes_reconcile_interval_seconds",
    int,
    600,
    group="usage",
    description="Self-heal interval for "
                "utils.usage_enforcement.reconcile_concurrent_processes "
                "(corrects tenant concurrent_processes_running drift). "
                "Default 10 min.",
)


def _module_available(module_name: str) -> bool:
    """CE build check (open-core/manifest.yaml `deny`) — the CE build strips
    whole service modules out of the tree. A lazy `from services.x import y`
    inside a job's broad `except Exception` silently swallows the resulting
    ImportError/ModuleNotFoundError and re-raises the same failure every
    tick forever (or, worse, an unguarded import at job-*registration* time
    crashes start_scheduler() outright, deregistering every job after it).
    Use this at registration time instead, so a denied module produces one
    clean "not in this edition" info line and the job is never scheduled.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def start_scheduler_watchdog():
    """δ §20.4 — recover from silent APScheduler death.

    Observed failure mode (Tim hit this three times on stage,
    2026-05-09/10): the uvicorn worker holding `mod:scheduler:leader`
    stops firing jobs (root cause undiagnosed — possibly worker
    recycle, signal, or an unhandled exception in another job). The
    lock-renewal job is itself an APScheduler job, so renewal stops
    too; the 120s TTL elapses; no other process re-acquires (each
    worker only ran `start_scheduler()` once at startup). Result:
    everything that depends on APScheduler — calibration coordinator,
    auto-retrain eval, build-farm canary, etc. — goes silent until
    an operator manually restarts api.

    This watchdog runs in EVERY uvicorn worker as a daemon thread.
    Every 30s it checks if the leader lock exists. If it doesn't
    (expired, no re-acquisition), the watchdog calls start_scheduler()
    here — which races other workers' watchdogs via SET NX EX, and
    whichever wins re-installs the scheduler in its own process.

    Idempotent — only spawns one thread per process.
    """
    global _watchdog_started
    if _watchdog_started:
        return

    # scale-tiered-workloads D3 — the watchdog only makes sense where the
    # scheduler itself runs. On a web replica that has handed background work to
    # a background sibling, do not spawn it (it would otherwise fight to
    # re-acquire the leader lock and re-install the scheduler in a web worker).
    # Default single-process web still passes this gate, so behaviour is
    # unchanged until the split is deployed.
    from utils.worker_sizing import should_run_background_work
    if not should_run_background_work():
        logger.info(
            "scheduler watchdog not started — background work runs on the "
            "background role, not this process")
        return

    _watchdog_started = True

    def _loop():
        import threading
        import time
        # Stagger initial check so 20 uvicorn workers don't all hit
        # Redis at the same instant on startup.
        time.sleep(int(os.getenv("MOD_SCHEDULER_WATCHDOG_INITIAL_DELAY_S", "30"))
                   + (os.getpid() % 30))
        check_interval = int(os.getenv("MOD_SCHEDULER_WATCHDOG_INTERVAL_S", "30"))
        while True:
            try:
                import redis
                r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
                holder = r.get("mod:scheduler:leader")
                if holder is None:
                    # Lock expired without re-acquisition. Try to grab
                    # it here. start_scheduler() does the SET NX EX
                    # atomically, so this is race-safe across workers
                    # — only one will actually proceed past the lock
                    # check, the rest no-op.
                    logger.warning(
                        "scheduler watchdog: leader lock missing — attempting "
                        "re-acquisition in pid=%d",
                        os.getpid(),
                    )
                    try:
                        start_scheduler()
                    except Exception as e:
                        logger.exception(
                            "scheduler watchdog: start_scheduler() raised: %s", e,
                        )
            except Exception as e:
                # Don't let a transient Redis blip kill the watchdog.
                logger.warning("scheduler watchdog: iteration failed: %s", e)
            time.sleep(check_interval)

    import threading
    t = threading.Thread(target=_loop, daemon=True, name="scheduler-watchdog")
    t.start()
    logger.info("scheduler watchdog started in pid=%d", os.getpid())


def _process_start_ticks(pid: int) -> str:
    """Kernel start time of `pid`, from /proc/<pid>/stat field 22.

    Distinguishes a live process from a recycled PID. Empty string where
    /proc is unavailable, which makes identity fall back to the PID alone —
    no worse than before.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
        # The comm field can contain spaces AND parentheses; split after the
        # LAST ')' so field offsets are correct for any process name.
        tail = data[data.rindex(")") + 2:].split()
        return tail[19]          # field 22 overall, 20th after comm
    except Exception:  # noqa: BLE001
        return ""


def _process_identity() -> str:
    """`<pid>:<start-ticks>` — unique across restarts and PID reuse."""
    pid = os.getpid()
    return f"{pid}:{_process_start_ticks(pid)}"


def _holder_is_alive(raw) -> bool:
    """Whether the recorded lock holder is still the process that took it."""
    try:
        value = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:  # noqa: BLE001
        return False

    pid_part, _, ticks = value.partition(":")
    try:
        pid = int(pid_part)
    except ValueError:
        return False

    try:
        os.kill(pid, 0)
    except OSError:
        return False          # gone

    if not ticks:
        # Written by an older build that stored a bare PID. Cannot verify, so
        # trust it and let the TTL be the backstop — the pre-existing
        # behaviour, kept so a rolling upgrade does not double-elect.
        return True
    return _process_start_ticks(pid) == ticks


#: Q9 part 1 — monotonic timestamp of the last Vault seal probe. The job is
#: registered as a 30s heartbeat and throttled to the (tunable) platform
#: setting `vault.seal_check_interval_seconds` here, so a cadence change takes
#: effect on the next heartbeat without a restart.
_vault_seal_last_probe: float = 0.0


def vault_seal_check_tick():
    """Q9 part 1 — probe Vault's seal state and alert on transitions.

    Registered once in start_scheduler() as a 30s heartbeat. On every run it
    re-reads the platform setting `vault.seal_check_interval_seconds`
    (default 60s): a value <= 0 disables the check (no restart needed),
    otherwise it only re-probes once at least that many seconds have elapsed
    since the last probe. The probe + transition state machine + one-shot
    VAULT_SEALED / VAULT_UNSEALED audit + admin alert live in
    services/vault_seal_monitor.check_vault_seal. Unreachable Vault is
    reported as "unknown", never as sealed, and never moves the last state.
    """
    # CE build: services/vault_seal_monitor.py is denied+stripped (open-core/
    # manifest.yaml), so the lazy import below would raise every tick.
    if not _module_available("services.vault_seal_monitor"):
        return None  # not in this edition — no monitor to probe
    global _vault_seal_last_probe
    try:
        import time as _t
        interval = int(_psr.get_setting("vault.seal_check_interval_seconds") or 0)
    except Exception as e:  # noqa: BLE001 — setting read failure: default cadence
        logger.warning("vault seal check: could not read interval setting: %s", e)
        interval = 60
    if interval <= 0:
        return None  # disabled via platform setting

    import time as _t
    now = _t.monotonic()
    if now - _vault_seal_last_probe < interval:
        return None  # throttle: heartbeat more often than the configured cadence
    _vault_seal_last_probe = now

    from database import SessionLocal
    db = SessionLocal()
    try:
        from services.vault_seal_monitor import check_vault_seal
        summary = check_vault_seal(db)
        # Only log the interesting cases; the steady no-transition tick is
        # too chatty at 1/min.
        if summary.get("transitioned") or summary.get("state") == "unknown":
            logger.info("vault seal check: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("vault seal check tick failed: %s", e)
        return None
    finally:
        db.close()


#: GSEC Q111 part 1/2 — monotonic stamps of the last clock-drift and
#: certificate-expiry passes. Both jobs are registered as 1-minute
#: heartbeats and throttled to their platform settings
#: (time.check_interval_minutes, default 10; tls.cert_expiry_warn_days is the
#: threshold, not a cadence), so a cadence change takes effect on the next
#: heartbeat without a restart.
_q111_last_clock_probe: float = 0.0
_q111_last_cert_probe: float = 0.0


def clock_skew_check_tick():
    """GSEC Q111 part 1 — clock-drift (NTP) check, throttled to
    `time.check_interval_minutes` (default 10 min).

    Measures worker heartbeat skew where the worker sent its own timestamp,
    falls back to DB now() vs the API clock, and raises one admin alert on
    the clear->breach transition of `time.max_clock_skew_seconds` (plus one
    on recovery). State machine + alert live in
    services/clock_skew_monitor.check_clock_skew. Only the scheduler leader
    reaches this job; nothing here adjusts a clock.
    """
    # CE build: services/clock_skew_monitor.py is denied+stripped (open-core/
    # manifest.yaml), so the lazy import below would raise every tick.
    if not _module_available("services.clock_skew_monitor"):
        return None  # not in this edition — no monitor to probe
    global _q111_last_clock_probe
    try:
        interval = int(_psr.get_setting("time.check_interval_minutes") or 0)
    except Exception as e:  # noqa: BLE001 — setting read failure: default cadence
        logger.warning("clock skew check: could not read interval setting: %s", e)
        interval = 10
    if interval <= 0:
        return None  # disabled via platform setting

    import time as _t
    now = _t.monotonic()
    if now - _q111_last_clock_probe < interval * 60:
        return None  # throttle: heartbeat more often than the configured cadence
    _q111_last_clock_probe = now

    from database import SessionLocal
    db = SessionLocal()
    try:
        from services.clock_skew_monitor import check_clock_skew
        summary = check_clock_skew(db)
        if summary.get("transitioned"):
            logger.info("clock skew check: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("clock skew check tick failed: %s", e)
        return None
    finally:
        db.close()


def cert_expiry_check_tick():
    """GSEC Q111 part 2 — certificate expiry check, throttled to
    `time.check_interval_minutes` (default 10 min).

    Probes the served leaf cert of each hostname in `tls.monitor_hostnames`
    (falling back to hosts derived from existing URL settings) and reads the
    internal CA / leaf certs under the startup tree read-only. Warns once per
    source when its not_after enters `tls.cert_expiry_warn_days` (default 21
    days) and once when it leaves. Read-only: MOD supports renewal, this tick
    never renews or rotates. Only the scheduler leader reaches this job.
    """
    # CE build: services/cert_expiry_monitor.py is denied+stripped (open-core/
    # manifest.yaml `CORE/API/services/cert_*.py`), so the lazy import below
    # would raise every tick.
    if not _module_available("services.cert_expiry_monitor"):
        return None  # not in this edition — no cert monitor to probe
    global _q111_last_cert_probe
    try:
        interval = int(_psr.get_setting("time.check_interval_minutes") or 0)
    except Exception as e:  # noqa: BLE001 — setting read failure: default cadence
        logger.warning("cert expiry check: could not read interval setting: %s", e)
        interval = 10
    if interval <= 0:
        return None  # disabled via platform setting

    import time as _t
    now = _t.monotonic()
    if now - _q111_last_cert_probe < interval * 60:
        return None  # throttle: heartbeat more often than the configured cadence
    _q111_last_cert_probe = now

    from database import SessionLocal
    db = SessionLocal()
    try:
        from services.cert_expiry_monitor import check_cert_expiry
        summary = check_cert_expiry(db)
        if summary.get("transitioned"):
            logger.info("cert expiry check: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("cert expiry check tick failed: %s", e)
        return None
    finally:
        db.close()


CONFORMANCE_INTERVAL_SETTING = "conformance.run_interval_minutes"


def conformance_run_interval_minutes() -> int:
    """GY.C12 — cadence (minutes) for the scheduled conformance run, from
    the platform setting `conformance.run_interval_minutes`. A value
    <= 0 means "disabled"; the registered APScheduler trigger still
    needs a positive interval, so it clamps to 1 minute heartbeat and
    `conformance_run_tick` itself no-ops while the setting is 0
    (disabling needs no restart)."""
    try:
        return max(1, int(_psr.get_setting(CONFORMANCE_INTERVAL_SETTING) or 0))
    except Exception:  # noqa: BLE001 — setting read failure: default cadence
        return 360


def conformance_run_tick():
    """GY.C12 — one scheduled pass of the continuous conformance engine
    (services.conformance.run_checks): every registered check runs
    isolated (exception → status error, the run never aborts), results
    persist to the append-only conformance_check_results table, and
    pass→fail drift emits a CONFORMANCE_DRIFT audit/security event.

    Registered once in start_scheduler(); like every other job there it
    only ever runs on the scheduler leader. Cadence is re-read from the
    platform setting on every tick, so
    `conformance.run_interval_minutes = 0` disables the job without a
    restart (on-demand runs via POST /platform/conformance/run are
    unaffected)."""
    try:
        interval = int(_psr.get_setting(CONFORMANCE_INTERVAL_SETTING) or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("conformance run: could not read interval setting: %s", e)
        return None
    if interval <= 0:
        return None  # disabled via platform setting

    from database import SessionLocal

    from utils.paid_import import optional_paid
    if optional_paid("services.conformance") is None:
        # FX.162.p5 — CE carve: no conformance engine, so the scheduled pass
        # does nothing (the setting-gated cadence above is untouched).
        return None
    db = SessionLocal()
    try:
        from services.conformance import run_checks

        summary = run_checks(db, trigger="schedule")
        db.commit()
        if summary.get("drift"):
            logger.warning(
                "conformance run %s: DRIFT on %s",
                summary["run_id"], summary["drift"])
        else:
            logger.info(
                "conformance run %s complete: %d checks",
                summary["run_id"], len(summary["results"]))
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("conformance run tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


# Q17 (owner decision, 2026-10-03) — sales-lead PII retention scrub.
# =========================================================================
#
# A sales lead (models.EnterpriseLead, captured from the pricing page's
# Enterprise "Contact Sales" form) carries personal fields: name, email and
# the free-text message. The row also carries non-personal context we want to
# KEEP for sales stats: id, created_at, status, tenant_id, and company.
#
# This daily tick scrubs the PII columns to NULL and stamps
# ``pii_scrubbed_at`` for a lead that is:
#   * OPEN (status not closed) and inactive >= leads.retention_open_inactive_months,
#     OR
#   * CLOSED and aged >= leads.retention_closed_months.
# "Inactive" = months since created_at (the lead table has no updated_at; the
# owner's rule was to fall back to max(updated_at, created_at) — here
# created_at IS the activity anchor, and there is no other activity column).
# A 0 month setting disables scrubbing for that branch (never).
#
# Exemptions (never scrubbed):
#   * a CONVERTED lead — one that became a customer (its tenant has a live
#     Subscription) — falls under customer-data rules instead;
#   * a lead already scrubbed (pii_scrubbed_at set) — idempotent;
#   * a lead under a LEGAL HOLD — services/legal_hold.is_held fails closed
#     (a lookup error counts as held, so a broken hold lookup can never
#     become an unpreserved scrub).
#
# The row is NEVER deleted (ISO 27001, no hard deletes) — only the PII
# columns are nulled. One AuditEvent per run records the counts + lead IDs
# (no PII). Runs when leads.retention_job_enabled is True (ON by default).


def _leads_retention_candidates(db):
    """Return the list of EnterpriseLead rows whose PII may be scrubbed this
    pass, applying the month windows from the platform settings. Exemptions
    (converted / already-scrubbed / legal-hold) are filtered per-row inside
    the tick, not here — this helper only encodes the age windows.

    Returns (leads, open_months, closed_months)."""
    open_months = int(_psr.get_setting("leads.retention_open_inactive_months",
                                       db=None) or 0)
    closed_months = int(_psr.get_setting("leads.retention_closed_months",
                                         db=None) or 0)
    now = datetime.now(timezone.utc)

    def _months_ago(months: int) -> datetime:
        # Approximate month arithmetic (30.44 days/month) — good enough for a
        # retention window; the boundary cases (11 vs 13 months) are far from
        # a month's ambiguity.
        return now - timedelta(days=months * 30.44)

    leads = []
    all_leads = (
        db.query(models.EnterpriseLead)
        .filter(models.EnterpriseLead.pii_scrubbed_at.is_(None))
        .all()
    )
    for lead in all_leads:
        status = (lead.status or "").lower()
        is_closed = status == "closed"
        if is_closed:
            if closed_months <= 0:
                continue
            if lead.created_at is None or lead.created_at > _months_ago(closed_months):
                continue
        else:
            if open_months <= 0:
                continue
            if lead.created_at is None or lead.created_at > _months_ago(open_months):
                continue
        leads.append(lead)
    return leads, open_months, closed_months


def _leads_retention_is_converted(db, lead) -> bool:
    """A lead is CONVERTED (became a customer) when its tenant has a live
    Subscription. Converted leads fall under customer-data retention rules,
    so the sales-lead scrub must skip them. A NULL-tenant lead is NOT
    converted (there is no customer to have become)."""
    if lead.tenant_id is None:
        return False
    try:
        sub = (
            db.query(models.Subscription)
            .filter(models.Subscription.tenant_id == lead.tenant_id)
            .first()
        )
    except Exception:  # noqa: BLE001 — treat lookup error as NOT converted
        # A broken subscription lookup must not cause us to scrub a lead we
        # can't confirm is safe; but the legal-hold gate below is the
        # authoritative fail-closed check. To be conservative we treat an
        # error as converted (skip) so we never scrub a possibly-customer.
        logger.warning(
            "leads_retention: subscription lookup failed for lead %s "
            "(tenant %s) — treating as converted (skipping)",
            lead.id, lead.tenant_id,
        )
        return True
    if sub is None:
        return False
    # A canceled / trialing subscription does not make them a live customer.
    return (sub.status or "").lower() not in ("canceled", "trialing")


def _run_leads_retention(db):
    """Q17 — scrub eligible sales-lead PII. Returns a dict of counts + ids
    (no PII). One audit row is emitted per run (by the caller)."""
    leads, open_months, closed_months = _leads_retention_candidates(db)
    scrubbed_ids = []
    held_skipped = 0
    converted_skipped = 0
    for lead in leads:
        lead_id = lead.id
        # Idempotency re-check (defensive; query already filtered NULL).
        if lead.pii_scrubbed_at is not None:
            continue
        # Converted lead — customer-data rules apply, not sales-lead rules.
        if _leads_retention_is_converted(db, lead):
            converted_skipped += 1
            continue
        # GY.C15 — legal hold: is_held fails closed (a lookup error counts as
        # held), so a broken hold lookup can never turn into an unpreserved
        # scrub. Skip held leads for the whole pass.
        try:
            from utils.paid_import import optional_paid
            _lhold = optional_paid("services.legal_hold")
            if _lhold is not None and _lhold.is_held(
                    db, tenant_id=lead.tenant_id, user_id=lead.user_id):
                held_skipped += 1
                logger.info(
                    "leads_retention: skipping lead %s (tenant %s) — legal "
                    "hold active; PII preserved", lead_id, lead.tenant_id,
                )
                continue
        except Exception as e:  # noqa: BLE001 — fail closed
            logger.error(
                "leads_retention: hold lookup raised for lead %s — failing "
                "closed (skipping whole batch): %s", lead_id, e,
            )
            return {
                "scrubbed": len(scrubbed_ids),
                "scrubbed_ids": scrubbed_ids,
                "held_skipped": held_skipped,
                "converted_skipped": converted_skipped,
                "open_months": open_months,
                "closed_months": closed_months,
                "hold_lookup_error": True,
            }
        # Scrub: NULL the PII columns. name/message -> NULL; email is
        # NOT NULL so it becomes the erasure marker (never a real address).
        lead.name = None
        lead.email = "[erased]"
        lead.message = None
        lead.pii_scrubbed_at = datetime.now(timezone.utc)
        scrubbed_ids.append(lead_id)
    return {
        "scrubbed": len(scrubbed_ids),
        "scrubbed_ids": scrubbed_ids,
        "held_skipped": held_skipped,
        "converted_skipped": converted_skipped,
        "open_months": open_months,
        "closed_months": closed_months,
        "hold_lookup_error": False,
    }


def leads_retention_tick():
    """Q17 — daily sales-lead PII retention scrub. Registered once in
    start_scheduler(); only the scheduler leader reaches it. Runs when
    ``leads.retention_job_enabled`` is True (ON by default).

    Scrubs eligible leads' name/email/message, stamps pii_scrubbed_at, keeps
    the row (ISO 27001), skips converted + legal-held leads, and writes one
    AuditEvent per run (counts + IDs, no PII)."""
    if not _psr.get_setting("leads.retention_job_enabled", db=None):
        logger.info("leads_retention: disabled (leads.retention_job_enabled=False)")
        return
    db = _get_db()
    try:
        summary = _run_leads_retention(db)
        if summary["hold_lookup_error"]:
            # Fail closed = skip the WHOLE batch: roll back any scrubs done
            # earlier in this pass so a broken hold lookup can never leave a
            # partial (unpreserved) scrub committed.
            db.rollback()
            summary["scrubbed"] = 0
            summary["scrubbed_ids"] = []
        db.commit()
        # One audit row per run — counts + IDs only, no PII.
        db.add(models.AuditEvent(
            tenant_id=None,
            user_id=None,
            event_name="LEADS_RETENTION_PII_SCRUBBED",
            details={
                "scrubbed": summary["scrubbed"],
                "scrubbed_ids": summary["scrubbed_ids"],
                "held_skipped": summary["held_skipped"],
                "converted_skipped": summary["converted_skipped"],
                "open_months": summary["open_months"],
                "closed_months": summary["closed_months"],
                "hold_lookup_error": summary["hold_lookup_error"],
            },
        ))
        db.commit()
        logger.info(
            "leads_retention: scrubbed=%d held=%d converted=%d",
            summary["scrubbed"], summary["held_skipped"],
            summary["converted_skipped"],
        )
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("leads_retention tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


# GSEC FX.39 (owner decision Q21=A, 2026-10-05) — billing-record retention.
# =========================================================================
#
# Billing records (models.BillingInvoice, PrepaidBalanceTopup,
# PrepaidBalanceRefund, CreditLedgerEntry — the tax figures live on the
# invoice row) are kept for the accounting obligation. Once a record is older
# than billing.record_retention_years (default 10) its PERSONAL fields are
# irreversibly crypto-purged; the row and every financial figure stay,
# anonymised. No hard delete (ISO 27001).
#
# The logic lives in services/privacy/billing_retention.py so the DSAR path
# and the replay script can reuse it; this tick only gates the setting, owns
# the transaction and writes the per-batch audit row.
#
# Legal hold (GY.C15) always wins: held tenants are skipped and a hold-lookup
# error fails closed (the whole batch is skipped). Off unless
# billing.retention_job_enabled is True.


def billing_retention_tick():
    """FX.39 — nightly billing-record retention purge. Registered once in
    start_scheduler(); only the scheduler leader reaches it. Off unless
    ``billing.retention_job_enabled`` is True."""
    if not False:
        logger.info("billing_retention: disabled "
                    "(billing.retention_job_enabled=False)")
        return
    _br = optional_paid("services.privacy.billing_retention")
    if _br is None:
        return
    db = _get_db()
    try:
        summary = _br.run_billing_retention(db)
        if summary["hold_lookup_error"]:
            # Fail closed: roll back so a broken hold lookup can never leave a
            # partial (unpreserved) purge committed.
            db.rollback()
            logger.error("billing_retention: hold lookup raised — batch skipped")
            return summary
        db.commit()
        # One audit row per batch — counts + row ids only, no PII.
        db.add(models.AuditEvent(
            tenant_id=None,
            user_id=None,
            event_name="BILLING_RETENTION_PII_PURGED",
            details={
                "retention_years": summary["years"],
                "purged": summary["purged"],
                "rows": summary["rows"],
                "held_skipped": summary["held_skipped"],
                "already_purged": summary["already_purged"],
                "keys_purged": summary["keys_purged"],
            },
        ))
        db.commit()
        logger.info(
            "billing_retention: window=%dy purged=%d held=%d already=%d "
            "keys=%d", summary["years"], summary["purged"],
            summary["held_skipped"], summary["already_purged"],
            summary["keys_purged"],
        )
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("billing_retention tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


# GSEC FX.92 (owner decision Q78=A) — hosted cancellation lifecycle
# (read-only export window after a SaaS subscription is canceled).
# The logic lives in services/cancellation_lifecycle.py; this tick only
# gates the setting and owns the transaction. Daily 04:10 UTC (between the
# FX.39 billing retention 03:20 and the retire-archived 04:00 jobs).
# Legal hold always blocks the end-of-window soft delete, and a
# hold-lookup error fails closed. On-prem (DEPLOYMENT_MODE != saas) the
# tick is a no-op. Off unless billing.cancellation_lifecycle_enabled is
# True.

def cancellation_lifecycle_tick():
    """FX.92 — daily hosted-cancellation lifecycle sweep. Registered once
    in start_scheduler(); only the scheduler leader reaches it. Off unless
    ``billing.cancellation_lifecycle_enabled`` is True."""
    if not False:
        logger.info("cancellation_lifecycle: disabled "
                    "(billing.cancellation_lifecycle_enabled=False)")
        return
    from utils.paid_import import optional_paid
    _cl = optional_paid("services.cancellation_lifecycle")
    if _cl is None:  # FX.162.p5f — CE carve: hosted cancellation is SaaS-only
        logger.info("cancellation_lifecycle: absent in this edition (CE carve)")
        return
    db = _get_db()
    try:
        summary = _cl.run_lifecycle_tick(db)
        if summary.get("ran"):
            logger.info(
                "cancellation_lifecycle: scanned=%d reminders=%d "
                "finalized=%d hold_blocked=%d errors=%d",
                summary["scanned"], summary["reminders_sent"],
                summary["finalized"], summary["hold_blocked"],
                summary["errors"],
            )
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("cancellation_lifecycle tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


# GSEC FX.126 (owner decision Q113=A) — daily asset-inventory alert tick.
# Flags every worker whose reported agent_version is older than the current
# release manifest (utils.worker_releases) and emits one audit row per
# outdated worker via the admin alert path (services.asset_inventory).
# Off unless assets.outdated_worker_alert_enabled is True. Daily 04:40 UTC
# (after the FX.92 cancellation 04:10 job).

def asset_outdated_worker_alert_tick():
    """FX.126 — daily outdated-worker alert sweep. Registered once in
    start_scheduler(); only the scheduler leader reaches it. Off unless
    ``assets.outdated_worker_alert_enabled`` is True."""
    if not _psr.get_setting("assets.outdated_worker_alert_enabled",
                            db=None):
        logger.info("asset_outdated_worker_alert: disabled "
                    "(assets.outdated_worker_alert_enabled=False)")
        return
    _fx126 = optional_paid("services.asset_inventory")
    if _fx126 is None:
        return
    db = _get_db()
    try:
        summary = _fx126.run_outdated_worker_alert_tick(db)
        if summary.get("flagged"):
            logger.info(
                "asset_outdated_worker_alert: flagged=%d outdated=%d",
                summary["flagged"], summary["outdated"],
            )
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("asset_outdated_worker_alert tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


def staff_register_due_email_tick():
    """GSEC FX.153 part 2 (Q140=B) — daily staff-register due email. Registered
    once in start_scheduler(); only the scheduler leader reaches it. Emails
    every active platform admin the current /due list (training-due +
    leaver-checklist-overdue). Off unless staff.due_email_enabled is
    True. Body + send live in services/staff_register.admin_email_tick."""
    if not _psr.get_setting("staff.due_email_enabled", db=None):
        logger.info("staff_register_due_email: disabled "
                    "(staff.due_email_enabled=False)")
        return
    _fx153 = optional_paid("services.staff_register")
    if _fx153 is None:
        return
    db = _get_db()
    try:
        summary = _fx153.admin_email_tick(db)
        if summary.get("sent"):
            logger.info("staff_register_due_email: sent=%d (training=%d "
                        "leavers=%d)",
                        summary["sent"], summary["training_overdue"],
                        summary["leavers_overdue"])
        return summary
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("staff_register_due_email tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


def paused_run_timeout_sweep_tick():
    """sz-2.9 wiring — paused-run timeout sweep (job id
    ``paused_run_timeout_sweep``, every 5 min). Registered once in
    start_scheduler(); only the scheduler leader reaches it. Own session, off
    the request path (services.paused_run_limits).

    The service's default cancel action only flips the run row, so this tick
    supplies a cancel_fn that ALSO marks the run's still-active ApiTaskQueue
    rows CANCELLED (same as the user-cancel endpoint,
    routers/workflows_domains/workflows.py) — otherwise a cancelled run could
    still have claimable work sitting in the queue. Never raises into the
    scheduler.
    """
    from sqlalchemy import update as sa_update
    from crud import workflows_domain as workflows_domain_crud
    from services import paused_run_limits as _sz29

    active_task_statuses = (
        models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
        models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING,
    )

    def _cancel_run_and_its_tasks(db, run_id):
        now = datetime.now(timezone.utc)
        flipped = workflows_domain_crud.conditional_cancel_run(
            db, run_id,
            completed_at=now,
            error_message="Cancelled automatically: paused run exceeded "
                          "workflows.paused_run_timeout_s (sz-2.9)",
        ).rowcount > 0
        if not flipped:
            return False  # lost race — leave the queue alone
        db.execute(
            sa_update(models.ApiTaskQueue)
            .where(
                models.ApiTaskQueue.workflow_run_id == run_id,
                models.ApiTaskQueue.status.in_(active_task_statuses),
            )
            .values(status=models.ApiTaskStatus.CANCELLED, completed_at=now)
        )
        db.commit()
        return True

    db = _get_db()
    try:
        cancelled = _sz29.sweep_expired_paused_runs(db,
                                                    cancel_fn=_cancel_run_and_its_tasks)
        if cancelled:
            logger.info("sz-2.9 paused-run sweep: cancelled run ids %s",
                        cancelled)
        return cancelled
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("sz-2.9 paused-run sweep tick failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        db.close()


def _drift_detection_tick():
    """wsip1.j2 wiring — drift sweep for every LIVE tenant (job id
    ``drift_detection_sweep``). services/drift_sweep.sweep_tenant flushes,
    never commits, so this tick commits per tenant; a failure on one tenant
    rolls back only that tenant's savepoint work and the sweep continues.
    Logs the exception CLASS name only (no SQL detail). Never raises.
    """
    from database import SessionLocal as _SL
    _drift = optional_paid("services.drift_sweep")
    if _drift is None:
        return
    db = _SL()
    try:
        tenant_ids = [
            row[0] for row in db.query(models.Tenant.id).filter(
                models.Tenant.deleted_at.is_(None),
            ).all()
        ]
        for tid in tenant_ids:
            try:
                events = _drift.sweep_tenant(db, tenant_id=tid)
                db.commit()
                # wsip1.k — publish strictly AFTER the commit succeeds, so a
                # realtime subscriber can never read an uncommitted event.
                _drift.publish_drift_events(events)
                if events:
                    logger.info("drift sweep tenant %s: %s event(s)",
                                tid, len(events))
            except Exception as exc:  # noqa: BLE001 — keep sweeping
                db.rollback()
                logger.warning("drift sweep tenant %s failed: %s",
                               tid, type(exc).__name__)
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("drift_detection_sweep tick raised: %s", e)
    finally:
        db.close()


def _oauth_refresh_tick():
    """sz-4.2c wiring — pre-expiry OAuth refresh sweep (job id
    ``oauth_refresh_tick``, every 60s). services/oauth_refresh.refresh_tick
    does its OWN single db.commit() (the one commit that releases the
    FOR UPDATE row locks), so this tick must NOT double-commit. Logs
    counters only when something non-zero happened. Never raises.
    """
    from database import SessionLocal as _SL
    from services import oauth_refresh as _oref
    db = _SL()
    try:
        counters = _oref.refresh_tick(db)
        if any(counters.values()):
            logger.info("oauth_refresh tick: %s", counters)
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        db.rollback()
        logger.exception("oauth_refresh tick raised: %s", e)
    finally:
        db.close()


def _domain_freshness_review_tick():
    """sz-6.17 wiring — quarterly domain-primitive freshness seeding (job id
    ``domain_freshness_review``, every 15 min). services/domain_freshness_review
    seeds idempotently, is gated by its own ENABLED_KEY, flushes only — so
    this tick commits. Logs only when items were created. Never raises.
    """
    from database import SessionLocal as _SL
    _dfr = optional_paid("services.domain_freshness_review")
    if _dfr is None:
        return
    db = _SL()
    try:
        counters = _dfr.seed_quarterly_review(db)
        db.commit()
        if counters.get("created", 0) > 0:
            logger.info("domain_freshness_review: %s created (%s)",
                        counters.get("created"), counters.get("quarter"))
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        db.rollback()
        logger.exception("domain_freshness_review tick raised: %s", e)
    finally:
        db.close()


def _human_approval_pending_gauge_tick():
    """expr 14.1 wiring — refresh human_approval_pending_count (job id
    ``expr_human_approval_pending_gauge``, every 5 min). services/human_approval_gauge
    counts PENDING_ACTION ManualInterventionState rows platform-wide and pushes
    the count into the Gauge. Read-only (begin_nested, no commit) so this tick
    never commits. Never raises.
    """
    from database import SessionLocal as _SL
    _carve_m = optional_paid("services.human_approval_gauge")
    if _carve_m is None:
        return
    refresh_human_approval_pending = getattr(_carve_m, "refresh_human_approval_pending")
    db = _SL()
    try:
        pending = refresh_human_approval_pending(db)
        if pending >= 0:
            logger.debug("expr_human_approval_pending_gauge: %s pending", pending)
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        db.rollback()
        logger.exception("expr_human_approval_pending_gauge tick raised: %s", e)
    finally:
        db.close()


def _workflow_certification_expiry_tick():
    """fortress 5.9 S4 — hourly certification expiry sweep (job id
    ``fortress_59_certification_expiry``, every 1 h). services/workflow_certifications
    expire_due flips live status='active' workflow_certifications rows whose
    expires_at <= now to 'expired' (+updated_at); expiry is a status, never a
    delete (ISO 27001 soft-delete only). Commits only when it flipped something.
    Never raises.
    """
    from database import SessionLocal as _SL
    _carve_m = optional_paid("services.workflow_certifications")
    if _carve_m is None:
        return
    expire_due = getattr(_carve_m, "expire_due")
    db = _SL()
    try:
        flipped = 0
        try:
            flipped = expire_due(db)
            if flipped > 0:
                db.commit()
                logger.info("fortress_59_certification_expiry: %s flipped", flipped)
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            db.rollback()
            logger.exception("fortress_59_certification_expiry tick raised: %s", e)
        notified = 0
        recipients: list[int] = []
        try:
            from services.workflow_certification_notifications import notify_expiring
            notified, recipients = notify_expiring(db)
            if notified > 0:
                db.commit()
                logger.info("fortress_59_certification_expiry: %s notified", notified)
        except Exception as e:  # noqa: BLE001 — notify must not undo the expiry commit
            db.rollback()
            logger.exception("fortress_59_certification_expiry notify raised: %s",
                             type(e).__name__)
        for uid in dict.fromkeys(recipients):
            try:
                from routers.notifications import publish_notification_event
                publish_notification_event(uid)
            except Exception as e:  # noqa: BLE001 — realtime push is best-effort
                logger.debug("fortress_59_certification_expiry publish raised: %s",
                             type(e).__name__)
        # fortress 9.1 — refresh the active-certification gauge AFTER the expiry
        # sweep so it reflects post-flip state. Platform-wide (no tenant label):
        # kind 'platform' = certifier_id NULL, 'third_party' = certifier_id set.
        # Every allowed kind is set (0 when absent) so a gauge never sticks at
        # a stale value. Telemetry: own try/except, never breaks the tick.
        try:
            from sqlalchemy import func as sa_func
            from utils.fortress_metrics import set_certifications_active
            from models import WorkflowCertification as _WC
            now = datetime.now(timezone.utc)
            rows = (db.query(_WC.certifier_id.is_(None),
                             sa_func.count())
                    .filter(_WC.status == "active",
                            _WC.deleted_at.is_(None),
                            _WC.expires_at > now)
                    .group_by(_WC.certifier_id.is_(None)).all())
            counts = {bool(is_platform): int(cnt) for is_platform, cnt in rows}
            set_certifications_active("platform", counts.get(True, 0))
            set_certifications_active("third_party", counts.get(False, 0))
            # fortress 9.3 — same sweep, expiring-window gauge: active + live
            # certs due within 90 days, cumulative per window (a cert due in
            # 5d counts in 7d, 30d AND 90d). Every (window, kind) pair is set
            # (0 when absent) so a gauge never sticks at a stale value.
            from utils.fortress_metrics import set_certifications_expiring
            rows_x = (db.query(_WC.certifier_id.is_(None), _WC.expires_at)
                      .filter(_WC.status == "active",
                              _WC.deleted_at.is_(None),
                              _WC.expires_at > now,
                              _WC.expires_at <= now + timedelta(days=90))
                      .all())
            exp_w = {True: [0, 0, 0], False: [0, 0, 0]}
            for is_platform, expires_at in rows_x:
                days_left = (expires_at - now).total_seconds() / 86400.0
                idxs = [i for i, lim in enumerate((7, 30, 90))
                        if days_left <= lim]
                for i in (idxs or [2]):
                    exp_w[bool(is_platform)][i] += 1
            for _wname, _wi in (("7d", 0), ("30d", 1), ("90d", 2)):
                set_certifications_expiring(_wname, "platform",
                                            exp_w[True][_wi])
                set_certifications_expiring(_wname, "third_party",
                                            exp_w[False][_wi])
        except Exception as e:  # noqa: BLE001 — gauge must not break the tick
            logger.debug("fortress_59_certification_expiry gauge refresh raised: %s",
                         type(e).__name__)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# wizard-knowledge-pack KP.7: tier -> auto-update check cadence. Mirrors
# services.uptime.SLA_TARGETS_BY_TIER / target_for_tier's tier-keyed-dict +
# lookup-helper pattern and its unknown-tier-falls-back-to-the-safe-default
# convention (there: None/no SLA; here: the slowest cadence).
# ---------------------------------------------------------------------------
_KP_UPDATE_CADENCE_DAYS_BY_TIER = {
    "enterprise": 1,
    "team": 7,
    "business": 7,
    "paid": 7,
}
_KP_UPDATE_CADENCE_DAYS_DEFAULT = 30  # unknown/None tier (incl. "community")

# In-process state for the KP.7 job (v1 skeleton simplification — see the
# job body's comment; no DB column for "last knowledge-pack check" exists).
_kp_last_checked: dict = {}  # tenant_id (or None) -> last-checked datetime
_kp_unreachable_logged = False  # log-once-per-process gate for air-gap info log


def _kp_update_due(tier, last_checked, now) -> bool:
    """Pure (no DB, no scheduler) — KP.7: is a plan-`tier` tenant due for a
    knowledge-pack update check, given its last check time `last_checked`
    (a tz-aware datetime or None) relative to `now`?

    `last_checked=None` (never checked this process) is always due.
    Otherwise due once `now - last_checked` reaches the tier's cadence in
    `_KP_UPDATE_CADENCE_DAYS_BY_TIER` (unknown/None tier uses the slowest/
    safest `_KP_UPDATE_CADENCE_DAYS_DEFAULT`)."""
    if last_checked is None:
        return True
    cadence_days = _KP_UPDATE_CADENCE_DAYS_BY_TIER.get(
        tier, _KP_UPDATE_CADENCE_DAYS_DEFAULT)
    return now - last_checked >= timedelta(days=cadence_days)


def start_scheduler():
    """Initialize and start the APScheduler. Called from main.py on startup.

    Uses a Redis lock so only ONE uvicorn worker runs the scheduler.
    Without this, each worker would register its own set of jobs.

    Idempotent — if APScheduler is already running in this process, the
    call is a no-op (just refreshes the lock TTL). If the previous
    scheduler is a zombie (instance exists but not running, e.g. after
    a watchdog-triggered re-acquisition), it's shut down before a fresh
    one is created.
    """
    from utils.paid_import import optional_paid
    global _scheduler

    # scale-tiered-workloads D3 — the APScheduler is background work and must
    # not run in a web request worker once the roles are split. Defense in depth:
    # main.py already gates the job_scheduler startup task, but start_scheduler()
    # is also reachable via the watchdog, so guard here too. Default
    # single-process web passes this gate (should_run_background_work() is True),
    # so today's single-container deploy is unchanged.
    from utils.worker_sizing import should_run_background_work
    if not should_run_background_work():
        logger.info(
            "start_scheduler skipped — background work runs on the background "
            "role, not this process")
        return

    # Idempotency: already running healthy in this process → just refresh
    # the lock TTL and return.
    if _scheduler is not None and getattr(_scheduler, "running", False):
        try:
            import redis
            r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
            r.expire("mod:scheduler:leader",
                     int(os.getenv("MOD_SCHEDULER_LOCK_TTL_S", "300")))
        except Exception:
            pass
        return

    # If we have a stale _scheduler object that isn't running, shut it
    # down cleanly so the fresh BackgroundScheduler below replaces it.
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("scheduler: shutdown of stale instance failed: %s", e)
        _scheduler = None

    # Acquire Redis lock — only one worker should run the scheduler
    try:
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        r = redis.from_url(redis_url)
        existing = r.get("mod:scheduler:leader")
        if existing is not None:
            # A bare PID cannot identify a process across a container restart.
            # PIDs are namespaced and restart from 1, so the recycled PID of a
            # dead leader is very often alive in the NEW container — os.kill()
            # then reports the stale lock as held and the scheduler never
            # starts until the TTL lapses. Harmless when that was 120s;
            # a five-minute blackout at the new TTL.
            #
            # The process start time makes the identity exact: a recycled PID
            # has a different one, so it can never be mistaken for the original.
            if not _holder_is_alive(existing):
                r.delete("mod:scheduler:leader")
                logger.info("Scheduler: cleared stale lock (holder=%s not alive)",
                            existing)
        acquired = r.set("mod:scheduler:leader", _process_identity(), nx=True,
                         ex=int(os.getenv("MOD_SCHEDULER_LOCK_TTL_S", "300")))
        if not acquired:
            logger.info("Scheduler already running in another worker — skipping")
            return
        logger.info("Scheduler leader elected (pid=%d)", os.getpid())
    except Exception as e:
        # Redis unavailable — allow scheduler to start (better than no scheduler)
        logger.warning("Could not acquire scheduler lock (Redis): %s — starting anyway", e)

    try:
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning("APScheduler not installed — scheduled jobs disabled. pip install apscheduler")
        return

    # APScheduler's defaults are wrong for this workload and were silently
    # discarding most job runs.
    #
    # Measured on the dev deployment 2026-07-31, over 30 minutes:
    #   Workflow trigger evaluation ...... 396 runs MISSED
    #   Worker maintenance ............... 277
    #   Calibration coordinator .......... 277
    #   Scheduler leader lock renewal .... 132
    # every one logged "was missed by 0:00:01.4".
    #
    # Three defaults combine to cause it:
    #
    #   max_workers=10   — this file registers 60 jobs, many on 30s intervals.
    #                      A handful of slow ones (each doing network or DB I/O)
    #                      occupy the whole pool and everything else queues.
    #   misfire_grace_time=1  — a queued job more than ONE SECOND late is not
    #                      run late, it is DISCARDED. Under any contention at
    #                      all, that is most of them.
    #   coalesce default — a backlog then fires as a stampede rather than once.
    #
    # This is the root cause of the failure documented in
    # start_scheduler_watchdog ("stops firing jobs ... root cause
    # undiagnosed"). `leader_lock_renewal` runs every 60s against a 120s TTL,
    # so TWO consecutive misses expire the lock — and it was missing far more
    # often than that. The scheduler was not dying; its renewal was being
    # thrown away, the lock lapsed, and another worker took over. The watchdog
    # was recovering from a self-inflicted wound every few minutes.
    #
    # For maintenance work, running LATE is almost always better than not
    # running: a 30s sweep that fires at 45s has done its job, while one that
    # is dropped has not.
    _scheduler = BackgroundScheduler(
        timezone="UTC",
        executors={
            "default": ThreadPoolExecutor(
                max_workers=int(os.getenv("MOD_SCHEDULER_MAX_WORKERS", "30"))),
        },
        job_defaults={
            # Run late rather than not at all.
            "misfire_grace_time": int(
                os.getenv("MOD_SCHEDULER_MISFIRE_GRACE_S", "300")),
            # A job that missed several runs fires ONCE on catch-up, not once
            # per missed interval — otherwise recovering from a stall means a
            # thundering herd on top of whatever caused the stall.
            "coalesce": True,
            # Guard against a slow job overlapping itself and consuming the
            # pool. Two concurrent runs is enough headroom for a long tick
            # without letting one job monopolise the executor.
            "max_instances": 2,
        },
    )

    # --- CE paid-module guard (1647a): wrap add_job on real scheduler instances.
    # MagicMock schedulers in tests have type().__module__ == "unittest.mock",
    # so they are left untouched (test_gy_c5_audit_gc_scheduled introspects
    # fake_sched.add_job.call_args_list).
    if type(_scheduler).__module__.startswith("apscheduler"):
        _orig_add_job = _scheduler.add_job

        def _ce_add_job(func, *a, **kw):  # type: ignore[no-untyped-def]
            _safe_job = _ce_guard_job(
                func, kw.get("id") or getattr(func, "__qualname__", str(func)))
            return _orig_add_job(_safe_job, *a, **kw)

        _scheduler.add_job = _ce_add_job

    # --- Remediation canary (every 15 min) ---
    _scheduler.add_job(
        _run_remediation_canary,
        IntervalTrigger(minutes=int(os.getenv("MOD_REMEDIATION_CANARY_MINUTES", "15"))),
        id="remediation_canary",
        name="Remediation pipeline canary (Foolproof #5)",
        replace_existing=True,
    )

    # --- GSEC FX.53 (Q36=A): MFA enforcement sweep (daily, 04:17 UTC) ---
    # Converge pass over in-scope users (platform/tenant admins + tenants
    # with auth.mfa_required_all_users + gov-profile installs): stamp the
    # 7-day grace start in Keycloak, then queue CONFIGURE_TOTP. Settings
    # changes trigger an immediate sweep via the platform_settings
    # after-commit seam, so this daily tick is the backstop that also
    # ages grace into enforced actions. Own session, off the request
    # path (services.mfa_enforcement.run_sweep).
    def _run_mfa_enforcement_sweep():
        from database import SessionLocal as _SL
        from keycloak_auth import keycloak_admin_client
        _fx53 = optional_paid("services.mfa_enforcement")
        if _fx53 is None:
            return
        db = _SL()
        try:
            counters = _fx53.run_sweep(db, keycloak_admin_client)
            if counters.get("checked"):
                db.commit()
                logger.info("FX.53 MFA enforcement sweep: %s", counters)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("FX.53 MFA enforcement sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.mfa_enforcement"):
        _scheduler.add_job(
            _run_mfa_enforcement_sweep,
            CronTrigger(hour=4, minute=17),
            id="mfa_enforcement_sweep",
            name="MFA enforcement grace/queue sweep (GSEC FX.53)",
            replace_existing=True,
        )
    else:
        logger.info("mfa_enforcement_sweep skipped (services.mfa_enforcement not in this edition)")

    # --- 2400GX-n8b (30.4.8b): daily purge of expired run-idempotency claims ---
    # Dedupe metadata only (workflow_run_idempotency_keys rows) — never runs.
    # purge_expired floors the window at 24h; a DB without the migration just
    # logs a warning per tick, it never crashes the scheduler.
    def _run_idempotency_purge():
        from database import SessionLocal as _SL
        from services.run_idempotency import purge_expired
        db = _SL()
        try:
            n = purge_expired(db)
            db.commit()
            logger.info("GX-n8b idempotency purge: %s key row(s) removed", n)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.warning("GX-n8b idempotency purge raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _run_idempotency_purge,
        CronTrigger(hour=3, minute=41),
        id="run_idempotency_purge",
        name="Purge expired workflow-run idempotency keys (GX-n8b)",
        replace_existing=True,
    )

    # --- wizard-knowledge-pack KP.7: auto-update check (daily, 06:30 UTC) ---
    # Periodic counterpart to the manual `POST /wizard/update-knowledge`
    # (routers/wizard_knowledge.py): polls the distribution manifest and
    # auto-applies a newer signed pack through the same kpc.* pipeline.
    # Gated by wizard_knowledge_settings.auto_update_enabled; cadence is
    # per-tier via _kp_update_due below.
    def _check_for_knowledge_pack_update():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("routers.wizard_knowledge")
        if _carve_m is None:
            return
        _KP_MANIFEST_URL = getattr(_carve_m, "DEFAULT_MANIFEST_URL")
        _kpc = optional_paid("services.knowledge_pack_consumer")
        if _kpc is None:
            return
        _wks = optional_paid("services.wizard_knowledge_settings")
        if _wks is None:
            return
        _st_mod = optional_paid("services.support_tickets")
        if _st_mod is None:
            return
        from services.support_tickets import _plan_tier_for_tenant as _kp_tier_for
        global _kp_unreachable_logged
        db = _SL()
        try:
            if not _wks.auto_update_enabled(db):
                return
            now = datetime.now(timezone.utc)
            tenant_ids = [row[0] for row in db.query(models.Tenant.id).all()]
            # v1 skeleton simplification (KP.3/KP.5-style, documented in
            # the KP.7 brief): no per-tenant "last knowledge-pack check"
            # DB column exists, so the due-check clock lives in this
            # in-process dict (resets on restart) rather than a migration.
            # Keyed by tenant_id when tenants exist; falls back to a single
            # platform-wide `None` key on a fresh/tenant-less install so
            # the job still runs somewhere.
            check_keys = tenant_ids or [None]
            due = [tid for tid in check_keys
                  if _kp_update_due(
                      _kp_tier_for(db, tid) if tid is not None else None,
                      _kp_last_checked.get(tid), now)]
            if not due:
                return
            manifest = _kpc.fetch_manifest(_KP_MANIFEST_URL, db)
            if manifest is None:
                # Unreachable/air-gapped is the expected default outcome
                # until KP.6's update server exists — log once per process,
                # never spam every tick.
                if not _kp_unreachable_logged:
                    logger.info(
                        "KP.7: knowledge-pack manifest unreachable "
                        "(air-gapped or no network) — will retry on the "
                        "next scheduled check")
                    _kp_unreachable_logged = True
            else:
                _kp_unreachable_logged = False
                version = manifest.get("version")
                current_version = _kpc._latest_applied_version(db)
                if current_version is None or _kpc._version_num(version) > \
                      _kpc._version_num(current_version):
                    pubkey = _wks.wizard_knowledge_pack_pubkey()
                    if _kpc.verify_signature(manifest, pubkey):
                        source_url = manifest.get("source_url")
                        pack_bytes = (_kpc.download_pack(source_url, db)
                                     if source_url else b"")
                        if _kpc.verify_content_hash(
                              pack_bytes, manifest.get("checksum")):
                            try:
                                pack = json.loads(pack_bytes)
                                if source_url is not None:
                                    pack.setdefault("source_url", source_url)
                                result = _kpc.apply_pack(
                                    db, pack, version, actor_user_id=None)
                                logger.info(
                                    "KP.7: auto-applied knowledge-pack %s (%s)",
                                    version, result.get("integrity_status"))
                            except (_kpc.KnowledgePackDowngradeError,
                                   _kpc.KnowledgePackReplayError):
                                pass
            for tid in due:
                _kp_last_checked[tid] = now
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            db.rollback()
            logger.exception(
                "KP.7 knowledge-pack auto-update check raised: %s", e)
        finally:
            db.close()

    if (_module_available("services.knowledge_pack_consumer")
            and _module_available("services.wizard_knowledge_settings")):
        _scheduler.add_job(
            _check_for_knowledge_pack_update,
            CronTrigger(hour=6, minute=30),
            id="knowledge_pack_auto_update",
            name="Wizard knowledge-pack auto-update check (KP.7)",
            replace_existing=True,
        )
    else:
        logger.info("knowledge_pack_auto_update skipped (services.knowledge_pack_consumer / services.wizard_knowledge_settings not in this edition)")

    # --- KP.10 — knowledge-pack reject/rollback-rate spike alerts (15 min) ---
    # Watches the rate of wizard_knowledge_pack_rejected / rolled_back audit
    # events; a spike means a bad pack got published or a customer install's
    # version/clock state is wrong. The tick opens its own short session and
    # never raises out (same shape as backup_alerts.backup_health_tick), so
    # the wrapper is just the lazy import (CE builds may deny the module).
    def _knowledge_pack_alert_tick():
        _carve_m = optional_paid("services.knowledge_pack_alerts")
        if _carve_m is None:
            return
        _tick = getattr(_carve_m, "knowledge_pack_alert_tick")
        _tick()

    if _module_available("services.knowledge_pack_alerts"):
        _scheduler.add_job(
            _knowledge_pack_alert_tick,
            IntervalTrigger(minutes=15),
            id="kp10_knowledge_pack_alerts",
            name="Knowledge-pack reject-spike alerts (KP.10)",
            replace_existing=True,
        )
    else:
        logger.info("kp10_knowledge_pack_alerts skipped (services.knowledge_pack_alerts not in this edition)")

    # --- GSEC FX.78 p3 (owner Q61=A): gov WebAuthn admin-rule sweep ---
    # Backstop for the security-key requirement Q61 puts on ADMINS under a
    # gov/DoD compliance profile (cmmc / fedramp_high_il5) or the explicit
    # auth.webauthn_required_for_admins switch: ages the mod_webauthn_grace_started
    # stamp into a queued WEBAUTHN_REGISTER required action in Keycloak.
    # Admin-only fan-out (much smaller than FX.53's all-users sweep), so it
    # ticks a few minutes after that one. No-op when the rule is off
    # (services.webauthn_policy.enforcement_relevant short-circuits).
    def _run_webauthn_admin_sweep():
        from database import SessionLocal as _SL
        from keycloak_auth import keycloak_admin_client
        _fx78p3 = optional_paid("services.webauthn_policy")
        if _fx78p3 is None:
            return
        db = _SL()
        try:
            counters = _fx78p3.run_sweep(db, keycloak_admin_client)
            if counters.get("checked"):
                db.commit()
                logger.info("FX.78 p3 gov WebAuthn admin sweep: %s", counters)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("FX.78 p3 gov WebAuthn sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.webauthn_policy"):
        _scheduler.add_job(
            _run_webauthn_admin_sweep,
            CronTrigger(hour=4, minute=29),
            id="webauthn_admin_sweep",
            name="Gov WebAuthn admin-rule grace/queue sweep (GSEC FX.78 p3)",
            replace_existing=True,
        )
    else:
        logger.info("webauthn_admin_sweep skipped (services.webauthn_policy not in this edition)")

    # --- GX.R3: run-completion webhook delivery sweep (every 30s) ---
    # Picks due RunWebhookDelivery rows (initial delivery + backoff retries)
    # and POSTs them with the HMAC signature. Own session, off the request
    # path; a no-op when nothing is due. Retries follow the exponential
    # ladder in services.run_completion_webhooks.
    def _run_webhook_delivery_tick():
        from database import SessionLocal as _SL
        from services.run_completion_webhooks import (
            run_webhook_delivery_tick as _tick,
        )
        try:
            counters = _tick(_SL)
            if counters.get("due"):
                logger.info("run_webhooks delivery tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("run_webhooks delivery tick raised: %s", e)

    _scheduler.add_job(
        _run_webhook_delivery_tick,
        IntervalTrigger(seconds=30),
        id="run_webhook_delivery",
        name="Run-completion webhook delivery (GX.R3)",
        replace_existing=True,
    )

    # --- GX.S7: scheduled HTTP trigger sweep (every 30s) ---
    # Claims due http_triggers rows (next_fire_at <= now) with
    # SELECT ... FOR UPDATE SKIP LOCKED so multiple API processes never
    # double-fire, then fires each (outbound HTTP happens OUTSIDE any open
    # DB transaction — see services.http_triggers). A no-op when nothing is
    # due.
    def _http_trigger_tick():
        from database import SessionLocal as _SL
        from services.http_triggers import run_trigger_tick as _ht_tick
        try:
            counters = _ht_tick(_SL)
            if counters.get("due"):
                logger.info("http_triggers sweep: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("http_triggers sweep raised: %s", e)

    _scheduler.add_job(
        _http_trigger_tick,
        IntervalTrigger(seconds=30),
        id="http_trigger_sweep",
        name="Scheduled HTTP trigger sweep (GX.S7)",
        replace_existing=True,
    )

    # --- wsip1.j2: drift detection sweep (cadence = drift.phase1.sweep_minutes,
    # read ONCE here; fallback 5 min) ---
    # Sweeps every LIVE tenant (deleted_at IS NULL); the service itself is
    # gated per tenant by drift_detection_enabled, so off tenants no-op.
    try:
        from services import drift_phase1_settings as _dp1
        # db=None: sweep_minutes opens/closes its own short session (the
        # cadence is read ONCE, when the job is added).
        _drift_sweep_min = _dp1.sweep_minutes(None, None)
    except Exception:  # noqa: BLE001 — cadence read failed: safe default
        _drift_sweep_min = 5
    if not isinstance(_drift_sweep_min, int) or _drift_sweep_min <= 0:
        _drift_sweep_min = 5
    if _module_available("services.drift_sweep"):
        _scheduler.add_job(
            _drift_detection_tick,
            IntervalTrigger(minutes=_drift_sweep_min),
            id="drift_detection_sweep",
            name="Production-drift detection sweep (wsip1.j2)",
            replace_existing=True,
        )
    else:
        logger.info("drift_detection_sweep skipped (services.drift_sweep not in this edition)")

    # --- sz-4.2c: OAuth pre-expiry refresh tick (every 60s) ---
    _scheduler.add_job(
        _oauth_refresh_tick,
        IntervalTrigger(seconds=60),
        id="oauth_refresh_tick",
        name="OAuth pre-expiry refresh sweep (sz-4.2c)",
        replace_existing=True,
    )

    # --- sz-6.17: quarterly domain-primitive freshness review seeding (15 min) ---
    if _module_available("services.domain_freshness_review"):
        _scheduler.add_job(
            _domain_freshness_review_tick,
            IntervalTrigger(minutes=15),
            id="domain_freshness_review",
            name="Quarterly domain-primitive freshness review (sz-6.17)",
            replace_existing=True,
        )
    else:
        logger.info("domain_freshness_review skipped (services.domain_freshness_review not in this edition)")

    # --- expr 14.1: human-approval-pending gauge refresh (5 min) ---
    if _module_available("services.human_approval_gauge"):
        _scheduler.add_job(
            _human_approval_pending_gauge_tick,
            IntervalTrigger(minutes=5),
            id="expr_human_approval_pending_gauge",
            name="Human-approval pending gauge (expr 14.1)",
            replace_existing=True,
        )
    else:
        logger.info("expr_human_approval_pending_gauge skipped (services.human_approval_gauge not in this edition)")

    # --- fortress 5.9 S4: workflow certification expiry sweep (1 h) ---
    if _module_available("services.workflow_certifications"):
        _scheduler.add_job(
            _workflow_certification_expiry_tick,
            IntervalTrigger(hours=1),
            id="fortress_59_certification_expiry",
            name="Workflow certification expiry sweep (fortress 5.9)",
            replace_existing=True,
        )
    else:
        logger.info("fortress_59_certification_expiry skipped (services.workflow_certifications not in this edition)")

    # --- Permission-update digest (every 15 min) ---
    # Drains pending_permission_emails for users whose local time is
    # in the 09:00-09:14 window. Pairs with the 15-min cadence so every
    # local 09:00 across every timezone lands inside one tick. Quiet
    # days = no emails. Replaces the per-event spam of "MOD permission
    # update" notifications for workflow_complete + permission_expiration.
    def _digest_tick():
        from database import SessionLocal as _SL
        from services.permission_digest import run_digest_tick
        try:
            counters = run_digest_tick(_SL)
            if counters.get("digests_sent", 0) or counters.get("errors", 0):
                logger.info("permission_digest tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("permission_digest tick raised: %s", e)

    _scheduler.add_job(
        _digest_tick,
        IntervalTrigger(minutes=15),
        id="permission_digest",
        name="Permission-update daily digest (09:00 user-local)",
        replace_existing=True,
    )

    # --- GSEC FX.77 p4: tenant s3_export log-streams (every 15 min) ---
    # For each enabled s3_export stream whose interval has elapsed since its
    # last_success_at, export the tenant's due AuditEvents (watermark =
    # last_success_at) to the customer's own S3 bucket as a signed JSON
    # object + detached .sig. Own session, off the request path.
    def _s3_export_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.log_stream_senders.s3_export")
        if _carve_m is None:
            return
        run_s3_export_tick = getattr(_carve_m, "run_s3_export_tick")
        db = _SL()
        try:
            counters = run_s3_export_tick(db)
            if counters.get("streams", 0) or counters.get("objects", 0):
                logger.info("s3_export tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("s3_export tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.log_stream_senders.s3_export"):
        _scheduler.add_job(
            _s3_export_tick,
            IntervalTrigger(minutes=15),
            id="s3_export_log_streams",
            name="Tenant s3_export log-streams (signed JSON to customer S3)",
            replace_existing=True,
        )
    else:
        logger.info("s3_export_log_streams skipped (services.log_stream_senders.s3_export not in this edition)")

    # --- Fixture drift audit (daily 04:10 UTC) ---
    # Audits the shared platform calibration bucket against its manifest;
    # on any mismatch flips ONLY 'ready' (non-deleted) tenants to
    # 'pending' via fixture_service.mark_drift (alert-only, GC.PFM.13b).
    def _fixture_drift_tick():
        # Lead review 2026-09-29: inert until the PFM.19 cutover publishes the
        # shared bucket, matching the calibration gate + create-tenant hook.
        from services.platform_settings import get_setting
        if not get_setting("fixture_shared_bucket_enabled"):
            return
        from database import SessionLocal as _SL
        from services import fixture_service
        from services.calibration_fixture_audit import audit_shared_bucket
        db = _SL()
        try:
            drift = audit_shared_bucket()
            if drift:
                ids = [
                    t.id
                    for t in db.query(models.Tenant).filter(
                        models.Tenant.fixture_status == "ready",
                        models.Tenant.deleted_at.is_(None),
                    ).all()
                ]
                flipped = fixture_service.mark_drift(db, ids)
                logger.info(
                    "fixture_drift tick: %d bad key(s) %s — %d tenant(s) marked drifting",
                    len(drift), drift, flipped,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("fixture_drift tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fixture_drift_tick,
        CronTrigger(hour=4, minute=10),
        id="fixture_drift",
        name="Shared calibration bucket drift audit (alert-only)",
        replace_existing=True,
    )

    # --- Hourly meter sampling (sz-payg.10, payg-billing-hardening §3.2) ---
    # Writes one BillingMeterSample row per (tenant, meter, sampled_at) for
    # the storage/concurrent state meters via utils.meter_sampler.sample_all,
    # which already sweeps every non-deleted tenant and logs+skips per-tenant
    # failures without aborting the batch — this tick (defined below, Job
    # implementations section) just owns the session and the admin on/off
    # switch. Only the scheduler leader (start_scheduler acquires the leader
    # lock before any job is added — see module docstring above) ever
    # reaches this, so multiple API replicas cannot double-fire it; the
    # sampler's ON CONFLICT DO NOTHING upsert is a second, redundant safety
    # net, not the primary guard.
    _scheduler.add_job(
        _meter_sample_tick,
        CronTrigger(minute=5),
        id="meter_sample",
        name="Hourly meter sampling (storage/concurrent, :05)",
        replace_existing=True,
    )

    # --- PAYG 4.6 auto-recharge sweep (every 5 min, payg-billing-hardening §5) ---
    # Off-session PaymentIntent on the saved card for tenants whose prepaid
    # balance fell under their auto_recharge_threshold_cents; the credit leg
    # is the payment_intent.succeeded webhook (task 332), so the 30-min
    # idempotency bucket in services/payg_auto_recharge.py is what keeps this
    # 5-min cadence from double-charging. Gated by the DB setting
    # billing.auto_recharge_sweep_enabled (default OFF = kill switch).
    _scheduler.add_job(
        _payg_auto_recharge_tick,
        IntervalTrigger(minutes=5),
        id="payg_auto_recharge",
        name="PAYG auto-recharge sweep (5 min)",
        replace_existing=True,
    )

    # --- PAYG spend-alerts sweep (every 15 min, sz-payg.20) ---
    # Soft-warn email per BillingSettings.soft_warn_thresholds_cents the
    # postpaid tenant's accrued period spend (utils.overage_cap.current_overage_cents)
    # has crossed, plus one hard-pause email past hard_pause_cents. Emails
    # ONLY — enforcement is a separate design item. usage_alerts' UsageAlertTracker
    # dedupes per threshold per billing period, so the 15-min cadence cannot
    # re-send. Gated by billing.payg_spend_alerts_enabled (default OFF).
    _scheduler.add_job(
        _payg_spend_alerts_tick,
        IntervalTrigger(minutes=15),
        id="payg_spend_alerts",
        name="PAYG spend alerts sweep (15 min)",
        replace_existing=True,
    )

    # --- Node-cost-benchmark daily refresh (daily 03:17, PR.5.10) ---
    # Re-derives node_cost_benchmarks rows from completed task history
    # using the trailing lookback_days window. Gated by
    # pricing.node_cost_benchmark_refresh_enabled (default OFF).
    if _module_available("services.node_cost_benchmark_runner"):
        _scheduler.add_job(
            _node_cost_benchmark_refresh_tick,
            CronTrigger(hour=3, minute=17),
            id="node_cost_benchmark_refresh",
            name="Node-cost-benchmark daily refresh (03:17)",
            replace_existing=True,
        )
    else:
        logger.info("node_cost_benchmark_refresh skipped (services.node_cost_benchmark_runner not in this edition)")

    # --- Subgraph equivalence analyzer daily (03:53, expr 3.5) ---
    # Scans non-deleted tenants for identical subgraphs across their
    # workflows. Gated by expr.subgraph_analyzer_job_enabled (default OFF).
    _scheduler.add_job(
        _subgraph_analyzer_tick,
        CronTrigger(hour=3, minute=53),
        id="subgraph_equivalence_analyzer",
        name="Subgraph equivalence analyzer daily (03:53)",
        replace_existing=True,
    )

    # --- WSIP 5.4 causal-graph nightly refresh (04:17) ---
    # Rebuilds the structural causal graph for every workflow with >= 50
    # production runs in the last 30d (wsip.causal_refresh_min_runs /
    # wsip.causal_refresh_window_days / wsip.causal_refresh_max_workflows).
    if _module_available("services.causal_graph_refresh"):
        _scheduler.add_job(
            _causal_graph_refresh_tick,
            CronTrigger(hour=4, minute=17),
            id="wsip_causal_graph_refresh",
            name="WSIP causal graph refresh nightly (04:17)",
            replace_existing=True,
        )
    else:
        logger.info("wsip_causal_graph_refresh skipped (services.causal_graph_refresh not in this edition)")

    # --- WSIP 3.x failure-prediction outcome sweep (every 15 min) ---
    # Fills actual_outcome on predictions whose run already reached a terminal
    # state (wsip.outcome_sweep_enabled kill switch, default OFF; the service
    # commits itself once per tick).
    if _module_available("services.failure_prediction_outcome_sweep"):
        _scheduler.add_job(
            _wsip_outcome_sweep_tick,
            IntervalTrigger(minutes=15),
            id="wsip_outcome_sweep",
            name="WSIP outcome sweep (every 15 min)",
            replace_existing=True,
        )
    else:
        logger.info("wsip_outcome_sweep skipped (services.failure_prediction_outcome_sweep not in this edition)")

    # --- expr 9.11 workflow-memory retention sweep (daily 03:38) ---
    # Prunes expired workflow-memory rows (workflow_memory.retention_sweep_enabled
    # kill switch, default OFF; the service commits itself).
    _scheduler.add_job(
        _workflow_memory_retention_tick,
        CronTrigger(hour=3, minute=38),
        id="workflow_memory_retention",
        name="Workflow-memory retention daily (03:38)",
        replace_existing=True,
    )

    # --- expr 9.6 workflow-checkpoint TTL sweep (daily 03:52) ---
    # Soft-deletes expired workflow-checkpoint rows (the service commits
    # itself).
    _scheduler.add_job(
        _workflow_checkpoint_retention_tick,
        CronTrigger(hour=3, minute=52),
        id="workflow_checkpoint_retention",
        name="Workflow-checkpoint TTL sweep daily (03:52)",
        replace_existing=True,
    )

    # --- WSIP 6.4 adversary findings collection sweep (every 10 min) ---
    # One bounded collect batch per tick, one commit, then post-commit finding
    # emits (wsip.adversary_collect_enabled kill switch, default OFF).
    if _module_available("services.adversary_collect_sweep"):
        _scheduler.add_job(
            _adversary_collect_sweep_tick,
            IntervalTrigger(minutes=10),
            id="wsip_adversary_collect",
            name="WSIP adversary collect sweep (every 10 min)",
            replace_existing=True,
        )
    else:
        logger.info("wsip_adversary_collect skipped (services.adversary_collect_sweep not in this edition)")

    # --- WSIP 6.3/6.4 adversary GA tick (every 10 min) ---
    # One generation per ACTIVE adversary pairing per tick, candidates handed to
    # adversary_dispatch (wsip.adversary_ga_enabled kill switch, default OFF;
    # the service commits itself per generation bump and per dispatched run).
    if _module_available("services.adversary_ga_tick"):
        _scheduler.add_job(
            _adversary_ga_tick,
            IntervalTrigger(minutes=10),
            id="wsip_adversary_ga",
            name="WSIP adversary GA tick (every 10 min)",
            replace_existing=True,
        )
    else:
        logger.info("wsip_adversary_ga skipped (services.adversary_ga_tick not in this edition)")

    # --- WSIP 3.2/3.3 nightly failure-predictor training (02:47) ---
    # One training pass over the recent event window: per-workflow models plus
    # the global fallback (wsip.predictor_training_enabled kill switch, default
    # OFF, read inside the service; the service commits itself per model).
    if _module_available("services.failure_predictor_training"):
        _scheduler.add_job(
            _failure_predictor_training_tick,
            CronTrigger(hour=2, minute=47),
            id="wsip_failure_predictor_training",
            name="WSIP failure-predictor training nightly (02:47)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("wsip_failure_predictor_training skipped (services.failure_predictor_training not in this edition)")

    # --- WSIP 6.1 adversary registrar reconcile (nightly 03:31) ---
    # The LIVE half of 6.1: nothing else in the app ever calls
    # services.adversary_workflows.reconcile_tenant, so pairings were only
    # ever created by unit tests. This tick converges every live tenant's
    # adversary pool to its production workflows — creates one live pairing
    # per eligible target (alive, non-system, non-archived, with at least
    # one run in the last 30 days) and tears down — never deletes — the
    # pairings whose target became ineligible/inactive. No new kill switch:
    # reconcile_tenant reads the existing wsip.adversary_enabled flag itself
    # (fail-closed; OFF pauses rather than tears down). One commit per
    # tenant, mirroring harbor_grant_reconcile.reconcile_tenant_repo_grants.
    if _module_available("services.adversary_workflows"):
        _scheduler.add_job(
            _wsip_adversary_reconcile_tick,
            CronTrigger(hour=3, minute=31),
            id="wsip_adversary_reconcile",
            name="WSIP adversary registrar reconcile nightly (03:31)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("wsip_adversary_reconcile skipped (services.adversary_workflows not in this edition)")

    # --- WSIP 8.2 autonomous canary monitor (every 5 min) ---
    # Evaluates open canary windows and records the promote/rollback/continue
    # decision (wsip.canary_monitor_enabled kill switch, default OFF; the
    # service commits itself per window and emits events post-commit).
    if _module_available("services.autonomous_canary_monitor"):
        _scheduler.add_job(
            _autonomous_canary_monitor_tick,
            IntervalTrigger(minutes=5),
            id="wsip_canary_monitor",
            name="WSIP canary monitor (every 5 min)",
            replace_existing=True,
        )
    else:
        logger.info("wsip_canary_monitor skipped (services.autonomous_canary_monitor not in this edition)")

    # --- WSIP 7.x autonomous operator poll (every 15 min) ---
    # Evaluates production workflows, scores them, and proposes
    # auto-fix actions. The service commits itself, so this wrapper
    # adds no second commit. pipeline_factory is left at its default.
    if _module_available("services.autonomous_operator"):
        _scheduler.add_job(
            _autonomous_operator_poll_tick,
            IntervalTrigger(minutes=15),
            id="wsip_operator_poll",
            name="WSIP autonomous operator poll (every 15 min)",
            replace_existing=True,
        )
    else:
        logger.info("wsip_operator_poll skipped (services.autonomous_operator not in this edition)")

    # --- Worker auto-rollout (every 5 min) ---
    # Publishing a new worker binary only flips the informational
    # `update_available`; the Go worker drains+self-updates on `update_requested`.
    # This tick auto-sets update_requested on out-of-date REMOTE workers as a
    # ROLLING update (concurrency cap + active-pool drain guard) so a new
    # version reaches remotes unattended without draining the fleet into an
    # outage. Embedded worker is updated by image redeploy, not here.
    # Disable: WORKER_AUTO_ROLLOUT_ENABLED=false.
    def _worker_auto_rollout_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.worker_auto_rollout")
        if _carve_m is None:
            return
        run_auto_rollout_tick = getattr(_carve_m, "run_auto_rollout_tick")
        try:
            result = run_auto_rollout_tick(_SL)
            if result.get("requested_online") or result.get("queued_offline") or result.get("error"):
                logger.info("worker_auto_rollout tick: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("worker_auto_rollout tick raised: %s", e)

    if _module_available("services.worker_auto_rollout"):
        _scheduler.add_job(
            _worker_auto_rollout_tick,
            IntervalTrigger(minutes=int(os.getenv("WORKER_AUTO_ROLLOUT_MINUTES", "5"))),
            id="worker_auto_rollout",
            name="Auto-rollout new worker versions (rolling, drain-guarded)",
            replace_existing=True,
        )
    else:
        logger.info("worker_auto_rollout skipped (services.worker_auto_rollout not in this edition)")

    # --- Work-loan expiry reaper (every 60s) --- GC.MWL.8
    # A borrower manager may go quiet mid-lease (crash, WAN cut, silent host)
    # and leave its loaned run un-collected. This sweep flips the owner's own
    # lapsed loans (lease_expires_at < now, status granted/running/claimed)
    # to `expired` on the LOCAL clock only — no peer call, no shared store —
    # and writes the MeshLoanEvent provenance row; the `expired` flip is what
    # lets the owner run the work itself (services.mesh_loans.owner_may_execute).
    # The reaper is idempotent (an expired row is never selected again), so a
    # double tick is a no-op.
    def _work_loan_expiry_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.mesh_loans")
        if _carve_m is None:
            return
        reap_expired_loans = getattr(_carve_m, "reap_expired_loans")
        db = _SL()
        try:
            reaped = reap_expired_loans(db)
            if reaped:
                logger.info("work_loan_expiry tick: reaped %d loan(s): %s",
                            len(reaped), reaped)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("work_loan_expiry tick raised: %s", e)
        finally:
            db.close()

    # GC.MWL.C7 — same cadence as the lease-expiry reap above, same
    # local-clock-only/no-peer-call posture, but its OWN job/session: resolve
    # every outstanding "handback" checkpoint export this manager owns one
    # step further (uploaded -> import registered -> loan rolled_back;
    # stuck-pending past the timeout -> plain cancel). Kept as a separate
    # job (not folded into _work_loan_expiry_tick above) so a failure here
    # never affects the lease-expiry reap's own session lifecycle.
    def _work_loan_handback_poll_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.mesh_loan_handback")
        if _carve_m is None:
            return
        poll_pending_handbacks = getattr(_carve_m, "poll_pending_handbacks")
        db = _SL()
        try:
            resolved = poll_pending_handbacks(db)
            if resolved:
                logger.info("work_loan_handback_poll tick: resolved %d handback(s): %s",
                            len(resolved), resolved)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("work_loan_handback_poll tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.mesh_loans"):
        _scheduler.add_job(
            _work_loan_expiry_tick,
            IntervalTrigger(seconds=60),
            id="work_loan_expiry",
            name="Work-loan lease expiry reaper (owner-side, local clock)",
            replace_existing=True,
        )
    else:
        logger.info("work_loan_expiry skipped (services.mesh_loans not in this edition)")

    if _module_available("services.mesh_loan_handback"):
        _scheduler.add_job(
            _work_loan_handback_poll_tick,
            IntervalTrigger(seconds=60),
            id="work_loan_handback_poll",
            name="Work-loan checkpoint hand-back poll (GC.MWL.C7, owner-side)",
            replace_existing=True,
        )
    else:
        logger.info("work_loan_handback_poll skipped (services.mesh_loan_handback not in this edition)")

    # GC.MWL.C7b — borrower-side periodic duplicate-execution re-check: for
    # every live loan THIS manager BORROWS, ask (or, locally in the
    # co-located posture, infer) whether the owner is ALSO executing the
    # same run and, if so, resolve the duplicate via
    # services.mesh_loan_duplicates.resolve_duplicate +
    # services.mesh_loan_handback.apply_duplicate_decision. Own job/session,
    # same posture as the two work-loan ticks above. Interval is the
    # platform setting `mesh.loan_duplicate_check_interval_s` (registered in
    # services/mesh_loan_duplicates.py, default 60s) — resolved once here at
    # scheduler start, same convention as the `fixture_shared_bucket_enabled`
    # lookup elsewhere in this function; no env var.
    def _work_loan_duplicate_check_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.mesh_loan_duplicate_check")
        if _carve_m is None:
            return
        detect_and_resolve_duplicates = getattr(_carve_m, "detect_and_resolve_duplicates")
        # GC.MWL.C7c — the real cross-manager peer_client
        # (services.mesh_loan_duplicate_check.http_peer_client). Imported
        # separately and tolerantly (not via a plain `from ... import`
        # alongside the line above) so a test double for this module that
        # predates http_peer_client still runs the co-located/local-state
        # fallback exactly as before, instead of an ImportError.
        try:
            from services.mesh_loan_duplicate_check import (
                http_peer_client as _make_peer_client,
            )
        except ImportError:
            _make_peer_client = None
        db = _SL()
        try:
            _kwargs = {"peer_client": _make_peer_client(db)} if _make_peer_client else {}
            resolved = detect_and_resolve_duplicates(db, **_kwargs)
            if resolved:
                logger.info("work_loan_duplicate_check tick: resolved %d loan(s): %s",
                            len(resolved), resolved)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("work_loan_duplicate_check tick raised: %s", e)
        finally:
            db.close()

    if (_module_available("services.mesh_loan_duplicate_check")
            and _module_available("services.mesh_loan_duplicates")):
        optional_paid("services.mesh_loan_duplicates")
        from services.platform_settings import get_setting as _get_duplicate_check_setting
        _duplicate_check_interval_s = _get_duplicate_check_setting(
            "mesh.loan_duplicate_check_interval_s")
        _scheduler.add_job(
            _work_loan_duplicate_check_tick,
            IntervalTrigger(seconds=_duplicate_check_interval_s),
            id="work_loan_duplicate_check",
            name="Work-loan duplicate-execution re-check (GC.MWL.C7b, borrower-side)",
            replace_existing=True,
        )
    else:
        logger.info("work_loan_duplicate_check skipped (services.mesh_loan_duplicate_check not in this edition)")

    # --- manager-work-loans (GC.MWL.3) — revocation gossip anti-entropy ---
    # A SCIM deactivate/reactivate already best-effort pushes its revocation
    # entry to every approved peer at emit time
    # (services/mesh_revocation_service.py:emit_local_change). This tick is
    # the retry backstop for a peer that was down/unreachable at that
    # moment: it re-pushes THIS manager's full locally-originated set to
    # every approved peer every ~60s (mirroring the work_loan_expiry tick
    # above), so a cut-off peer still converges once it comes back — no
    # per-peer inflight/retry bookkeeping needed, since re-pushing the
    # current state is always idempotent (CRDT last-writer-wins merge).
    def _mesh_revocation_anti_entropy_tick():
        from database import SessionLocal as _SL
        mesh_revocation_service = optional_paid("services.mesh_revocation_service")
        if mesh_revocation_service is None:
            return
        db = _SL()
        try:
            pushed = mesh_revocation_service.anti_entropy_push(db)
            if pushed:
                logger.debug("mesh_revocation anti-entropy tick: pushed %d entrie(s)", pushed)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("mesh_revocation anti-entropy tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.mesh_revocation_service"):
        _scheduler.add_job(
            _mesh_revocation_anti_entropy_tick,
            IntervalTrigger(seconds=60),
            id="mesh_revocation_anti_entropy",
            name="Revocation gossip anti-entropy push (cut-off peer backstop)",
            replace_existing=True,
        )
    else:
        logger.info("mesh_revocation_anti_entropy skipped (services.mesh_revocation_service not in this edition)")

    # --- llm-stack-wake-idle §2 — reclaim idle managed-LLM capacity ---
    # A GPU sits idle roughly 95% of the time and costs money 100% of it. Runs
    # in DRY RUN until LLM_IDLE_ENABLED=true: it reports what it would stop,
    # which is the only honest way to let an operator see its judgement before
    # it can take their assistant offline.
    def _llm_idle_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.llm_idle")
        if _carve_m is None:
            return
        reclaim_once = getattr(_carve_m, "reclaim_once")
        db = _SL()
        try:
            reclaim_once(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("llm_idle tick raised: %s", e)
        finally:
            db.close()

    # GT.D6: services.llm_idle is Paid (open-core manifest deny) and absent
    # from the CE tree — guard at registration so the tick is never scheduled
    # instead of re-raising an ImportError every tick.
    if _module_available("services.llm_idle"):
        _scheduler.add_job(
            _llm_idle_tick,
            IntervalTrigger(minutes=5),
            id="llm_idle_reclaim",
            name="Managed-LLM idle reclamation",
            replace_existing=True,
        )
    else:
        logger.info("llm_idle_reclaim skipped (services.llm_idle not in this edition)")

    # --- Ephemeral-NIM idle reclamation (backstop for event-driven sleep-back) ---
    # sleep_back fires on /complete, /failed and cache-hit completion, but a NIM
    # can be stranded ON by a run that errored before its NIM node, and the
    # one-per-GPU competitor eviction that used to double as turn-off was removed
    # once NIMs could co-reside. This sweep sleeps any wake-owned NIM that no
    # PENDING/RUNNING task needs (release_stack's own lookahead + wake-ownership
    # guards make it safe to run during live workflows).
    def _nim_idle_tick():
        from database import SessionLocal as _SL
        # CE carve (FIX-CE-CARVE-GUARD-NIM-EPHEMERAL-IMPORTS): services/nim_ephemeral.py
        # is a manifest `deny:` row, so the bare `from services import nim_ephemeral`
        # below the tick's opening raised ImportError on every 2-min tick (the later
        # `except Exception` never protected the import line itself). Same optional_paid
        # posture as the _llm_idle_tick above: CE has no ephemeral NIM, so the tick is a
        # silent no-op; paid behaviour is unchanged when the module exists.
        _nim = optional_paid("services.nim_ephemeral")
        if _nim is None:
            return
        db = _SL()
        try:
            _nim.reclaim_idle(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("nim_idle tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _nim_idle_tick,
        IntervalTrigger(minutes=2),
        id="nim_idle_reclaim",
        name="Ephemeral-NIM idle reclamation",
        replace_existing=True,
    )

    # GA.GSW.4d — tasks dispatched while their user-assigned stack was still
    # waking carry `awaiting_stack` and are refused by the claim hold_filter.
    # This sweep re-runs ensure for them and clears the marker once a fit exists.
    def _stack_held_redrive_tick():
        from database import SessionLocal as _SL
        from services import stack_wake as _sw
        db = _SL()
        try:
            _sw.redrive_held_tasks(db)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("stack_held_redrive tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _stack_held_redrive_tick,
        IntervalTrigger(seconds=30),
        id="stack_held_redrive",
        name="Stack-held task re-driver",
        replace_existing=True,
    )

    # --- scheduled-pricing applier — replaces the deleted Airflow ---
    # `apply_scheduled_pricing_dag.py` (Airflow purge, commit a35c9743e). The
    # DAG (every 5 min) POSTed /private/pricing/apply-scheduled; both were
    # deleted with no in-process replacement, so every grace-period /
    # future-dated pricing change ("Changes will be applied by scheduled job")
    # and every approved-but-scheduled proposal was silently NEVER applied.
    # This sweep is that missing job. Idempotent (applied rows/proposals are
    # flagged / flipped to APPLIED) and best-effort (one bad row is skipped,
    # not fatal). ON by default; MOD_SCHEDULED_PRICING_APPLY_ENABLED=false
    # disables it (fixing a silent-drop bug, so the default is on).
    def _scheduled_pricing_tick():
        if os.getenv("MOD_SCHEDULED_PRICING_APPLY_ENABLED", "true").lower() not in (
                "1", "true", "yes", "on"):
            return
        from database import SessionLocal as _SL
        _pricing = optional_paid("services.admin_plans_pricing_service")
        if _pricing is None:
            return
        db = _SL()
        try:
            _pricing.apply_due_scheduled_pricing(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("scheduled_pricing tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.admin_plans_pricing_service"):
        _scheduler.add_job(
            _scheduled_pricing_tick,
            IntervalTrigger(minutes=int(os.getenv("MOD_SCHEDULED_PRICING_APPLY_MINUTES", "5"))),
            id="scheduled_pricing_apply",
            name="Apply due scheduled pricing changes",
            replace_existing=True,
        )
    else:
        logger.info("scheduled_pricing_apply skipped (services.admin_plans_pricing_service not in this edition)")

    # --- persistent-stack-billing §3.4 — meter running-stack reservations ---
    # A persistent stack reserves cpu/mem/GPU 24/7 and denies it to the pool;
    # this prices that reservation × elapsed × rate and deducts credits through
    # CreditManager. Anchored on metered_through_at so a missed tick self-heals
    # and a double tick is a no-op. SHIPS DARK: with reservation_metering_enabled
    # false / rates 0 it advances the anchor, logs the would-charge amount, and
    # moves zero credits — inert on a normal upgrade until the operator sets
    # rates and flips the flag. A per-stack error is logged, not fatal.
    def _stack_billing_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.stack_billing")
        if _carve_m is None:
            return
        meter_once = getattr(_carve_m, "meter_once")
        db = _SL()
        try:
            meter_once(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("stack_billing tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.stack_billing"):
        _scheduler.add_job(
            _stack_billing_tick,
            IntervalTrigger(minutes=int(os.getenv("STACK_BILLING_TICK_MINUTES", "5"))),
            id="stack_reservation_billing",
            name="Persistent-stack reservation metering",
            replace_existing=True,
        )
    else:
        logger.info("stack_reservation_billing skipped (services.stack_billing not in this edition)")

    # --- GH.4 — meter burst-worker sessions + drain/teardown at zero balance ---
    # Anchored on BurstSession.last_debited_at, same idempotent-tick shape as
    # the stack-reservation meter above. The switch is the platform setting
    # `burst.metering_enabled` (default ON since B2); no env var gates
    # behaviour here — an unreadable setting resolves to ON (teardown safety).
    def _burst_metering_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.burst_metering")
        if _carve_m is None:
            return
        debit_tick = getattr(_carve_m, "debit_tick")
        db = _SL()
        try:
            debit_tick(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("burst_metering tick raised: %s", e)
        finally:
            db.close()

    # GH.5/CE — the switch is the platform setting `burst.metering_enabled`
    # (B2 registered it; DB > env > default), not an env var. A read failure
    # resolves to ON: this tick is the net that drains + tears burst
    # instances down, so it must fail toward cleaning up, never toward
    # leaving a cloud instance running unmanaged.
    try:
        _burst_metering_on = bool(_psr.get_setting("burst.metering_enabled"))
    except Exception:  # noqa: BLE001 — unreadable setting -> ON (teardown safety)
        _burst_metering_on = True
    if _burst_metering_on:
        if _module_available("services.burst_metering"):
            _scheduler.add_job(
                _burst_metering_tick,
                IntervalTrigger(minutes=int(os.getenv("BURST_METERING_TICK_MINUTES", "5"))),
                id="burst_session_metering",
                name="Burst-worker session metering (holds + per-tick debit)",
                replace_existing=True,
            )
        else:
            logger.info("burst_session_metering skipped (services.burst_metering not in this edition)")
    else:
        logger.info("burst_session_metering disabled (burst.metering_enabled off)")

    # --- GH.6 — reconcile MOD-tagged burst instances against the worker
    # registry, terminating orphans no known enrollment claims. The switch is
    # the platform setting `burst.reconciler_enabled` (default
    # ON since B2); no env var gates behaviour here, and an unreadable
    # setting resolves to ON — this sweep terminates orphan cloud instances.
    def _burst_reconciler_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.burst_reconciler")
        if _carve_m is None:
            return
        reconcile_all = getattr(_carve_m, "reconcile_all")
        db = _SL()
        try:
            reconcile_all(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("burst_reconciler tick raised: %s", e)
        finally:
            db.close()

    # Same switch shape as the metering tick above: the platform setting
    # `burst.reconciler_enabled`, and an unreadable setting resolves to ON —
    # this sweep terminates orphan cloud instances, so it must fail toward
    # cleanup, never toward leaving spend running.
    try:
        _burst_reconciler_on = bool(_psr.get_setting("burst.reconciler_enabled"))
    except Exception:  # noqa: BLE001 — unreadable setting -> ON (teardown safety)
        _burst_reconciler_on = True
    if _burst_reconciler_on:
        if _module_available("services.burst_reconciler"):
            _scheduler.add_job(
                _burst_reconciler_tick,
                IntervalTrigger(minutes=int(os.getenv("BURST_RECONCILER_TICK_MINUTES", "5"))),
                id="burst_orphan_reconciler",
                name="Burst orphan-instance reconciliation",
                replace_existing=True,
            )
        else:
            logger.info("burst_orphan_reconciler skipped (services.burst_reconciler not in this edition)")
    else:
        logger.info("burst_orphan_reconciler disabled (burst.reconciler_enabled off)")

    # --- CE burst bounds — scale idle burst sessions back down -------------
    # `burst_auto.scale_down_idle` had no production caller, so an idle cloud
    # instance stayed up forever and the only bound on spend was the per-cycle
    # instance cap. Same `_carve_m` shape as the metering + reconciler ticks
    # above. The switch is the SAME platform setting the scale-UP controller
    # already uses (`burst.scale_enabled`), read through burst_auto's existing
    # fail-closed `_scale_controller_enabled()` helper — so CE and Pro behave
    # identically, and an unreadable setting means nothing tears down. Cadence
    # reuses `BURST_METERING_TICK_MINUTES`: this sweep belongs to the same
    # burst net, and it adds no new tunable.
    # A raising sweep is logged, never fatal — the metering tick above must
    # keep draining/tearing at zero balance regardless of this one.
    _burst_scale_down_on = False
    if _module_available("services.burst_auto"):
        try:
            from services import burst_auto as _burst_auto_gate
            _burst_scale_down_on = bool(_burst_auto_gate._scale_controller_enabled())
        except Exception:  # noqa: BLE001 — unreadable setting -> OFF (fail closed)
            _burst_scale_down_on = False

    def _burst_scale_down_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.burst_auto")
        if _carve_m is None:
            return
        scale_down_idle = getattr(_carve_m, "scale_down_idle")
        db = _SL()
        try:
            closed = scale_down_idle(db)
            if closed:
                logger.info("burst_scale_down closed sessions: %s", closed)
        except Exception as e:  # noqa: BLE001 — never break the metering tick
            logger.exception("burst_scale_down sweep raised: %s", e)
        finally:
            db.close()

    if _burst_scale_down_on:
        if _module_available("services.burst_auto"):
            _scheduler.add_job(
                _burst_scale_down_tick,
                IntervalTrigger(minutes=int(os.getenv("BURST_METERING_TICK_MINUTES", "5"))),
                id="burst_scale_down",
                name="Burst idle-instance scale-down (bounded cloud spend)",
                replace_existing=True,
            )
        else:
            logger.info("burst_scale_down skipped (services.burst_auto not in this edition)")
    else:
        logger.info("burst_scale_down disabled (burst.scale_enabled off or unreadable)")

    # --- managed-ai-surfaces-and-stack-ops §9.1 — scheduled stack backups ---
    # Backup previously existed only as a button, which means it existed only
    # for as long as someone remembered to press it.
    #
    # ON by default, unlike the two sweeps above. Those stop or move a running
    # service, so shipping them dark is the honest default; this one only ever
    # adds a snapshot. The cost of it running unexpectedly is some MinIO
    # storage, against a cost of it not running that is a customer's data.
    #
    # Ticks every 15 minutes and each stack is due on its own interval, so the
    # tick rate bounds lateness (a 24h schedule fires within 15 minutes of its
    # mark) without the sweep itself deciding how often anything is backed up.
    def _stack_backup_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.stack_backup")
        if _carve_m is None:
            return
        backup_sweep_once = getattr(_carve_m, "backup_sweep_once")
        db = _SL()
        try:
            backup_sweep_once(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("stack backup sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.stack_backup"):
        _scheduler.add_job(
            _stack_backup_tick,
            IntervalTrigger(minutes=15),
            id="stack_backup_sweep",
            name="Scheduled persistent-stack backups",
            replace_existing=True,
        )
    else:
        logger.info("stack_backup_sweep skipped (services.stack_backup not in this edition)")

    # --- GSEC FX.84 part 2b — scheduled PLATFORM (on-prem) backup sets -----
    # Same shape as stack_backup_sweep: the tick cadence only bounds lateness,
    # the due decision lives in services.platform_backup_runner (cron from
    # backup.schedule_cron vs the last run recorded in backup_status.json,
    # read-only). The run itself is PLANNED, not executed — the API process
    # has no host-exec seam (see that module's docstring + the design doc),
    # so this tick records the command a seam would exec.
    def _platform_backup_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.platform_backup_runner")
        if _carve_m is None:
            return
        sweep_once = getattr(_carve_m, "sweep_once")
        db = _SL()
        try:
            result = sweep_once(db)
            if result.get("planned"):
                logger.info(
                    "[platform_backup_sweep] planned %s (retention=%s days)",
                    " ".join(result["command"]), result["env"]["RETENTION_DAYS"],
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("platform backup sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.platform_backup_runner"):
        _scheduler.add_job(
            _platform_backup_tick,
            IntervalTrigger(minutes=15),
            id="platform_backup_sweep",
            name="Scheduled on-prem platform backup sets",
            replace_existing=True,
        )
        # --- GSEC FX.58 p1b — hourly DB snapshots -------------------------
        # Same seam as the nightly sweep: PLANS `backup.sh --snapshot`
        # (due when the last snapshot in backup_status.json is >1h old),
        # never executes. Cadence 5min bounds lateness of the 1h target.
        def _platform_snapshot_tick():
            from database import SessionLocal as _SL
            _carve_m = optional_paid("services.platform_backup_runner")
            if _carve_m is None:
                return
            plan_snapshot_run = getattr(_carve_m, "plan_snapshot_run")
            db = _SL()
            try:
                result = plan_snapshot_run(db)
                if result.get("planned"):
                    logger.info(
                        "[platform_snapshot_sweep] planned %s",
                        " ".join(result["command"]),
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("platform snapshot sweep raised: %s", e)
            finally:
                db.close()

        _scheduler.add_job(
            _platform_snapshot_tick,
            IntervalTrigger(minutes=5),
            id="platform_snapshot_sweep",
            name="Scheduled hourly platform DB snapshots",
            replace_existing=True,
        )
    else:
        logger.info("platform_backup_sweep skipped (services.platform_backup_runner not in this edition)")

    # --- GSEC FX.58 p2a [B] — WEEKLY automatic restore proof (Q91=A) ------
    # Same seam shape as the two sweeps above: due-ness lives in
    # services/restore_test_job (newest restore_test line in the erasure
    # ledger vs backup.restore_test_interval_days, default 7 = weekly per
    # Q91, gated by backup.restore_test_enabled), the tick only bounds
    # lateness. The script (CORE/startup/restore-test.sh, held from FX.150)
    # runs on the MANAGER host against docker, so the API has no exec seam
    # for it — the tick PLANS the invocation and logs it, exactly like
    # platform_backup_sweep/plan_snapshot_run do today; a host-exec seam
    # would pass that command to run_restore_test(runner=...). Recording is
    # the erasure-ledger restore_test line + one SYSTEM ApiTaskQueue row per
    # attempt (started/finished/ok/duration/backup tested), which FX.58 p2b
    # reads as audit evidence. Cadence 1h bounds lateness of a weekly target.
    def _restore_test_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.restore_test_job")
        if _carve_m is None:
            return
        sweep_once = getattr(_carve_m, "sweep_once")
        db = _SL()
        try:
            result = sweep_once(db)
            if result.get("planned"):
                logger.info(
                    "[fx58p2a_restore_test] planned %s (interval=%s days)",
                    " ".join(result["command"]), result["interval_days"],
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("restore-test sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.restore_test_job"):
        _scheduler.add_job(
            _restore_test_tick,
            IntervalTrigger(hours=1),
            id="fx58p2a_restore_test",
            name="Weekly automatic restore proof (FX.58 p2a, Q91=A)",
            replace_existing=True,
        )
    else:
        logger.info("fx58p2a_restore_test skipped (services.restore_test_job not in this edition)")

    # --- GSEC FX.84 part 4 [B] — backup-health admin alerts ---------------
    # Hourly: reads the SAME readings part 2a shows (backup_status.json +
    # the restore-test ledger) and raises one deduped SecurityAlert per
    # problem (last run failed / no success in 48h / gov profile wants an
    # off-box copy / gov profile wants a fresh restore test) through the
    # security-alert engine + immediate platform-admin email. Inert on
    # SaaS (no on-prem backup set); the tick itself never raises.
    def _backup_health_tick():
        _carve_m = optional_paid("services.backup_alerts")
        if _carve_m is None:
            return
        _tick = getattr(_carve_m, "backup_health_tick")
        _tick()

    if _module_available("services.backup_alerts"):
        _scheduler.add_job(
            _backup_health_tick,
            IntervalTrigger(hours=1),
            id="fx84_backup_health",
            name="On-prem backup health alerts (FX.84 p4)",
            replace_existing=True,
        )
    else:
        logger.info("fx84_backup_health skipped (services.backup_alerts not in this edition)")

    # --- stack-ha-reconciliation §4.1 — desired-vs-observed reconciliation ---
    # The only thing in the system that acts on a stack gap. Runs in DRY RUN
    # until STACK_RECONCILER_LIVE=true (§4.9): it reports exactly what it would
    # do, which is the only honest way to let an operator see its judgement
    # before it can move their services.
    def _stack_reconcile_tick():
        from database import SessionLocal as _SL
        db = _SL()
        try:
            _carve_m = optional_paid("services.stack_reconciler")
            if _carve_m is None:
                return
            reconcile_once = getattr(_carve_m, "reconcile_once")
            reconciler_enabled = getattr(_carve_m, "reconciler_enabled")
            if reconciler_enabled(db):
                try:
                    reconcile_once(db)
                except Exception as e:  # noqa: BLE001
                    logger.exception("stack_reconciler tick raised: %s", e)
            # managed-AI-self-deploy: bring the managed LLM up when a GPU is
            # available and nothing is deployed yet (deploy-if-absent). Runs
            # regardless of the stack-reconciler flag — it is conservative and
            # idempotent; the deploy it dispatches is retried by the reconciler
            # above (when enabled) until the worker serves it.
            # GT.D6 DS-5: managed-AI deploy-if-absent lives in the Paid module
            # (services.persistent_stacks_paid_service, DS-1). In the CE build
            # that module is stripped from the tree, so this call is skipped
            # here and the rest of the tick (stale-deploy release, upgrade and
            # streaming-migration evaluation) still runs. The whole tick is
            # already unregistered in CE by the registration guard below; this
            # guard is what keeps the paid-tree call pointed at the paid module.
            _paid_stacks = optional_paid("services.persistent_stacks_paid_service")
            if _paid_stacks is None:
                logger.info(
                    "managed-AI reconcile skipped (services.persistent_stacks_paid_service "
                    "not in this edition)")
            else:
                try:
                    _paid_stacks.reconcile_managed_ai(db)
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    logger.exception("managed-AI reconcile tick raised: %s", e)
            # stack-stop-release §4 — self-heal backstop that releases GPU
            # reservations held by deploy/migrate tasks whose stack is
            # stopped/torn-down or was superseded by a newer deploy. Runs
            # regardless of the stack-reconciler flag, same reasoning as
            # reconcile_statuses above: it only stops a stale reservation,
            # never re-places anything.
            try:
                from services.stack_reconciler import reconcile_stale_deploy_tasks
                reconcile_stale_deploy_tasks(db)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                logger.exception("stack stale-deploy-task reconcile tick raised: %s", e)
            # 2400GX-S10c4d7 — the other half of the S10c4b claim guard: when
            # no connected worker advertises file_secrets, a stamped deploy
            # stays unclaimed forever. Same tick, same reason field; never
            # cancels, never re-places.
            try:
                from services.stack_reconciler import reconcile_unplaceable_file_secret_deploys
                reconcile_unplaceable_file_secret_deploys(db)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                logger.exception("stack unplaceable-file-secret reconcile tick raised: %s", e)
            # GA.ASW.7 — safe stack upgrades are a pollable state machine
            # (services/stack_upgrade.evaluate_upgrade), and nothing else polls
            # them: without this tick a bad pin would sit in `deploying` forever
            # until someone happened to call the upgrade endpoint. Best-effort,
            # per-stack exception-isolation is already inside the sweep.
            # GT.D4: services.stack_upgrade is Paid (open-core manifest deny) and
            # absent from the CE tree — guard so the tick skips cleanly instead of
            # re-raising (and logging) an ImportError every tick.
            if _module_available("services.stack_upgrade"):
                try:
                    from services import stack_upgrade as _su
                    _su.evaluate_pending_upgrades(db)
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    logger.exception("stack upgrade evaluation tick raised: %s", e)
            else:
                logger.debug("stack_upgrade tick skipped (not in this edition)")
            # GA.ASW.10.8 — streaming zero-downtime DB migrations are the same
            # pollable-state-machine shape as GA.ASW.7's upgrades
            # (services/stack_streaming_migration.evaluate_streaming_migration),
            # and nothing else polls them: without this tick a migration would
            # sit in prepare/seeding/replicating/verify/cutting_over forever
            # unless an operator happened to poll the migrate-streaming
            # endpoints. Same best-effort, per-stack exception isolation as the
            # upgrade hook immediately above.
            # GT.D4: services.stack_streaming_migration is Paid (open-core
            # manifest deny) and absent from the CE tree — guard so the tick
            # skips cleanly instead of re-raising an ImportError every tick.
            if _module_available("services.stack_streaming_migration"):
                try:
                    from services import stack_streaming_migration as _ssm
                    _ssm.evaluate_pending_streaming_migrations(db)
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    logger.exception("stack streaming migration evaluation tick raised: %s", e)
            else:
                logger.debug("stack_streaming_migration tick skipped (not in this edition)")
        finally:
            db.close()

    # GT.D6: services.stack_reconciler is Paid (open-core manifest deny) and
    # absent from the CE tree — guard the whole tick (HA/failover restore,
    # managed-AI deploy-if-absent, stale-deploy release) so it is never
    # scheduled in CE instead of re-raising an ImportError every tick.
    if _module_available("services.stack_reconciler"):
        _scheduler.add_job(
            _stack_reconcile_tick,
            IntervalTrigger(minutes=2),
            id="persistent_stack_reconciler",
            name="Persistent-stack reconciliation (desired vs observed)",
            replace_existing=True,
        )
    else:
        logger.info("persistent_stack_reconciler skipped (services.stack_reconciler not in this edition)")

    # GSEC FX.65 p3a — BYOK revoke -> tenant "dark" detector. Health-probes
    # every tenant with a kms.byok_provider configured; a refusal marks
    # that tenant dark (services.kms_tenant_state.record_unwrap). Cadence
    # from the platform setting kms.health_interval_minutes (default 2),
    # read once at registration — same literal-interval-from-setting
    # pattern as the other jobs here.
    if _module_available("services.kms_tenant_state"):
        try:
            _kms_health_minutes = max(
                1, int(_psr.get_setting("kms.health_interval_minutes") or 2))
        except Exception:  # noqa: BLE001 — setting read failure: default cadence
            _kms_health_minutes = 2
        _kms_tenant_state = optional_paid("services.kms_tenant_state")
        if _kms_tenant_state is None:
            return
        _scheduler.add_job(
            _kms_tenant_state.check_all_tick,
            IntervalTrigger(minutes=_kms_health_minutes),
            id="kms_tenant_health_check",
            name="BYOK tenant key health probe (revoke -> dark)",
            replace_existing=True,
        )

    # --- add-persistent-stacks §6 — hourly persistent-stack rebalancer ---
    # Scores each running stack's worker vs. the best alternative and triggers a
    # blue/green migration when a peer is >= STACK_REBALANCE_THRESHOLD better.
    #
    # §4.7 — SUBORDINATE to reconciliation, not a peer of it. Restoring a stack
    # that is running nowhere beats relocating one that is merely running in a
    # suboptimal place, and the two must not act on the same stack in the same
    # window: the rebalancer would start a blue/green migration of a stack the
    # reconciler is already re-placing, producing two deploys for one stack.
    # So this defers entirely while any gap is outstanding.
    def _stack_rebalance_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.stack_rebalancer")
        if _carve_m is None:
            return
        rebalance_once = getattr(_carve_m, "rebalance_once")
        _carve_m = optional_paid("services.stack_reconciler")
        if _carve_m is None:
            return
        find_gaps = getattr(_carve_m, "find_gaps")
        db = _SL()
        try:
            gaps = find_gaps(db)
            if gaps:
                logger.info(
                    "stack_rebalancer: deferring, %d stack(s) not running "
                    "anywhere — restore before relocate", len(gaps))
                return
            moves = rebalance_once(db)
            if moves:
                logger.info("stack_rebalancer tick: %d move(s)", len(moves))
        except Exception as e:  # noqa: BLE001
            logger.exception("stack_rebalancer tick raised: %s", e)
        finally:
            db.close()

    # GT.D6: services.stack_rebalancer is Paid (open-core manifest deny) and
    # absent from the CE tree (its stack_reconciler.find_gaps guard import is
    # denied too) — guard at registration so the tick is never scheduled in CE.
    if _module_available("services.stack_rebalancer"):
        _scheduler.add_job(
            _stack_rebalance_tick,
            IntervalTrigger(hours=1),
            id="persistent_stack_rebalancer",
            name="Hourly persistent-stack rebalancer",
            replace_existing=True,
        )
    else:
        logger.info("persistent_stack_rebalancer skipped (services.stack_rebalancer not in this edition)")

    # --- add-self-service-sso §4.4 — domain verify sweep ---
    # Runs every minute. The per-row schedule (60s for pending in
    # first hour, 5min for pending after / for flap-monitoring,
    # hourly for healthy verified) is enforced by the WHERE clause
    # in the sweep — a single tick rate gives us all four cadences
    # without registering four jobs.
    def _sso_sweep_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.sso_domain_sweep")
        if _carve_m is None:
            return
        run_sweep_tick = getattr(_carve_m, "run_sweep_tick")
        try:
            counters = run_sweep_tick(_SL)
            if counters.get("checked"):
                logger.info("sso_domain_sweep tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("sso_domain_sweep tick raised: %s", e)

    if _module_available("services.sso_domain_sweep"):
        _scheduler.add_job(
            _sso_sweep_tick,
            IntervalTrigger(minutes=1),
            id="sso_domain_verify_sweep",
            name="SSO domain DNS verification sweep",
            replace_existing=True,
        )
    else:
        logger.info("sso_domain_verify_sweep skipped (services.sso_domain_sweep not in this edition)")

    # --- add-self-service-sso §6.4 — Keycloak realm event collector ---
    # Pulls IDENTITY_PROVIDER_* events from Keycloak's admin /events
    # endpoint and translates them into sso_login_audit rows. Cursor
    # lives in the sso_login_audit table itself (MAX(created_at) WHERE
    # source='kc_event_collector') so no separate state.
    def _sso_login_audit_collector_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.sso_login_audit_collector")
        if _carve_m is None:
            return
        run_collector_tick = getattr(_carve_m, "run_collector_tick")
        try:
            counters = run_collector_tick(_SL)
            if counters.get("ingested"):
                logger.info("sso_login_audit_collector tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("sso_login_audit_collector tick raised: %s", e)

    if _module_available("services.sso_login_audit_collector"):
        _scheduler.add_job(
            _sso_login_audit_collector_tick,
            IntervalTrigger(minutes=1),
            id="sso_login_audit_collector",
            name="SSO realm-event collector → sso_login_audit",
            replace_existing=True,
        )
    else:
        logger.info("sso_login_audit_collector skipped (services.sso_login_audit_collector not in this edition)")

    # --- add-scim-directory-sync §3.8 — Keycloak reconcile ---
    # Heals MOD↔Keycloak state drift for SCIM-provisioned users.
    # 5-minute cadence matches the spec's "within 5 min on restart"
    # guarantee for the chaos-test scenario.
    def _scim_reconcile_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.scim_reconcile")
        if _carve_m is None:
            return
        run_reconcile_tick = getattr(_carve_m, "run_reconcile_tick")
        try:
            counters = run_reconcile_tick(_SL)
            if counters.get("scanned"):
                logger.info("scim_reconcile tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("scim_reconcile tick raised: %s", e)

    if _module_available("services.scim_reconcile"):
        _scheduler.add_job(
            _scim_reconcile_tick,
            IntervalTrigger(minutes=5),
            id="scim_reconcile",
            name="SCIM ↔ Keycloak reconciliation",
            replace_existing=True,
        )
    else:
        logger.info("scim_reconcile skipped (services.scim_reconcile not in this edition)")

    # --- add-scim-directory-sync §3.2 — idempotency cache sweep ---
    # Nightly cleanup of expired idempotency records. The WHERE-clause
    # gate at lookup time is what makes idempotency CORRECT; this
    # sweep is purely for table-size hygiene.
    from apscheduler.triggers.cron import CronTrigger

    def _scim_idempotency_sweep():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("scim.idempotency")
        if _carve_m is None:
            return
        sweep_expired = getattr(_carve_m, "sweep_expired")
        try:
            db = _SL()
            try:
                deleted = sweep_expired(db)
                if deleted:
                    logger.info(
                        "scim_idempotency sweep deleted %d expired rows",
                        deleted,
                    )
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("scim_idempotency sweep raised: %s", e)

    _scheduler.add_job(
        _scim_idempotency_sweep,
        CronTrigger(hour=3, minute=17),  # 03:17 UTC
        id="scim_idempotency_sweep",
        name="SCIM idempotency-cache expired-row sweep",
        replace_existing=True,
    )

    # --- add-scim-directory-sync §5.6 — sustained-failure webhook ---
    def _scim_webhook_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.scim_activity_jobs")
        if _carve_m is None:
            return
        run_webhook_tick = getattr(_carve_m, "run_webhook_tick")
        try:
            counters = run_webhook_tick(_SL)
            if counters.get("fired"):
                logger.info("scim_failure_webhook tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("scim_failure_webhook tick raised: %s", e)

    if _module_available("services.scim_activity_jobs"):
        _scheduler.add_job(
            _scim_webhook_tick,
            IntervalTrigger(minutes=5),
            id="scim_failure_webhook",
            name="SCIM sustained-failure webhook trigger",
            replace_existing=True,
        )
    else:
        logger.info("scim_failure_webhook skipped (services.scim_activity_jobs not in this edition)")

    # --- add-scim-directory-sync §5.8 — nightly archive ---
    def _scim_archive_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.scim_activity_jobs")
        if _carve_m is None:
            return
        run_archive_tick = getattr(_carve_m, "run_archive_tick")
        try:
            counters = run_archive_tick(_SL)
            if counters.get("archived"):
                logger.info("scim_activity_archive: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("scim_activity_archive raised: %s", e)

    if _module_available("services.scim_activity_jobs"):
        _scheduler.add_job(
            _scim_archive_tick,
            CronTrigger(hour=4, minute=0),  # 04:00 UTC
            id="scim_activity_archive",
            name="SCIM activity nightly archive (90d+) → MinIO",
            replace_existing=True,
        )
    else:
        logger.info("scim_activity_archive skipped (services.scim_activity_jobs not in this edition)")

    # --- GSEC FX.33 — WORM audit archive (nightly 04:20 UTC) ---
    # Exports audit rows older than audit.online_retention_months to the
    # Object-Lock archive bucket (append-only NDJSON + integrity manifest).
    # Nothing is ever hard-deleted from the online tables. Gated by
    # audit.archive_enabled + audit.archive_bucket (re-read every run).
    def _audit_archive_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.audit_archive")
        if _carve_m is None:
            return
        run_archive_tick = getattr(_carve_m, "run_archive_tick")
        try:
            result = run_archive_tick(_SL)
            if result and result.get("status") not in ("disabled",):
                logger.info("audit_worm_archive: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("audit_worm_archive tick raised: %s", e)

    if _module_available("services.audit_archive"):
        _scheduler.add_job(
            _audit_archive_tick,
            CronTrigger(hour=4, minute=20),  # 04:20 UTC
            id="audit_worm_archive",
            name="WORM audit archive (FX.33: online_retention_months+ → "
                 "Object-Lock bucket, append-only)",
            replace_existing=True,
        )
    else:
        logger.info("audit_worm_archive skipped (services.audit_archive not in this edition)")

    # --- FX.57 part 1 (owner Q40=A) — system log archive (nightly 03:55
    # UTC) ---
    # Exports wizard_run_logs rows older than logs.searchable_days to
    # the platform system-logs bucket (gzip JSONL + SHA-256 manifest),
    # verifies the upload, and ONLY then soft-marks the rows
    # archived_at. Rows are NEVER hard-deleted (ISO 27001). Gated by
    # logs.archive_job_enabled (re-read every run).
    def _system_log_archive_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.system_log_archive")
        if _carve_m is None:
            return
        run_log_archive_tick = getattr(_carve_m, "run_log_archive_tick")
        try:
            result = run_log_archive_tick(_SL)
            if result and result.get("status") not in ("disabled",):
                logger.info("system_log_archive: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("system_log_archive tick raised: %s", e)

    if _module_available("services.system_log_archive"):
        _scheduler.add_job(
            _system_log_archive_tick,
            CronTrigger(hour=3, minute=55),  # 03:55 UTC
            id="system_log_archive",
            name="System log archive (FX.57: searchable_days+ → gzip JSONL "
                 "+ manifest, verified, soft-mark)",
            replace_existing=True,
        )
    else:
        logger.info("system_log_archive skipped (services.system_log_archive not in this edition)")

    # --- Harbor quota reconcile sweep (daily 02:00 UTC) ---
    # Belt-and-suspenders against drift: every plan-change path
    # already calls sync_tenant_harbor_quota_from_plan synchronously,
    # but a sweep catches:
    #   - A site that bypassed the helper (manual DB UPDATE, future
    #     code path that forgot to call sync, etc.)
    #   - A transient Harbor outage that swallowed a sync at the time
    #     of the plan change
    #   - A plan's max_storage_gb being edited (Sandbox 5 GB -> 10 GB)
    #     — every tenant on that plan needs the new cap propagated
    # Idempotent — no-op when quota already matches. Runs at 02:00
    # UTC so it doesn't compete with the 09:00-local digest window.
    def _harbor_quota_reconcile_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.harbor_quota_reconcile")
        if _carve_m is None:
            return
        run_reconcile_tick = getattr(_carve_m, "run_reconcile_tick")
        try:
            counters = run_reconcile_tick(_SL)
            logger.info("harbor_quota_reconcile tick: %s", counters)
        except Exception as e:  # noqa: BLE001
            logger.exception("harbor_quota_reconcile tick raised: %s", e)

    if _module_available("services.harbor_quota_reconcile"):
        _scheduler.add_job(
            _harbor_quota_reconcile_tick,
            CronTrigger(hour=2, minute=0),
            id="harbor_quota_reconcile",
            name="Harbor storage-quota reconcile against active plan (daily 02:00 UTC)",
            replace_existing=True,
        )
    else:
        logger.info("harbor_quota_reconcile skipped (services.harbor_quota_reconcile not in this edition)")

    # --- Harbor paid-repo pull-grant reconcile (every 15 min, SaaS only) ---
    # GF.14a (launch-completion-megaplan, gf-commerce). Paid images live in
    # one Harbor project per paid listing; every tenant's robot must be able
    # to pull exactly the repos its ACTIVE node/stack entitlements map to.
    # Converges the ``mod-paid/*`` grants (grant missing, revoke extra) and
    # touches no other permission. SaaS-only because on-prem has no paid
    # catalog; the sweep is idempotent and cheap at steady state.
    if os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas" and \
            _module_available("services.harbor_grant_reconcile"):
        def _harbor_grant_reconcile_tick():
            from database import SessionLocal as _SL
            _carve_m = optional_paid("services.harbor_grant_reconcile")
            if _carve_m is None:
                return
            reconcile_tenant_repo_grants = getattr(_carve_m, "reconcile_tenant_repo_grants")
            try:
                db = _SL()
                try:
                    counters = reconcile_tenant_repo_grants(db)
                    logger.info("harbor_grant_reconcile tick: %s", counters)
                finally:
                    db.close()
            except Exception as e:  # noqa: BLE001
                logger.exception("harbor_grant_reconcile tick raised: %s", e)

        _scheduler.add_job(
            _harbor_grant_reconcile_tick,
            IntervalTrigger(minutes=15),
            id="harbor_grant_reconcile",
            name="Harbor paid-repo pull-grant reconcile (every 15 min)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    # GT.D4: services.harbor_grant_reconcile is Paid (open-core manifest deny)
    # and absent from the CE tree — the registration above is skipped so the
    # job never runs (and never logs a missing-module error) in CE.
    elif os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas":
        logger.debug("harbor_grant_reconcile skipped (not in this edition)")

    # --- Failure pattern report (item #11) — weekly aggregate of
    # failed builds: top categories, top libraries, top
    # (category, library) pairs. Surfaces which libraries are
    # consistently failing so operators can curate fixes.
    # CE build: services.failure_pattern_report is denied and stripped from
    # the tree, so the old unguarded module-level import below would raise
    # ModuleNotFoundError here and crash start_scheduler() outright — taking
    # down every job registered after this point, not just this one.
    if _module_available("services.failure_pattern_report"):
        _carve_m = optional_paid("services.failure_pattern_report")
        if _carve_m is None:
            return
        _fpr_tick = getattr(_carve_m, "run_report_tick")
        def _failure_pattern_report_tick():
            from database import SessionLocal as _SL
            _fpr_tick(_SL)
        _scheduler.add_job(
            _failure_pattern_report_tick,
            CronTrigger(day_of_week="sun", hour=3, minute=0),
            id="failure_pattern_report",
            name="Weekly failure-pattern aggregate (item #11)",
            replace_existing=True,
        )
    else:
        logger.info("failure_pattern_report skipped (services.failure_pattern_report not in this edition)")

    # --- Docker disk monitor (every 15 min) — pre-emptive cleanup of
    # build cache + unused images before /var/lib/docker fills and
    # builds start dying with "No space left on device". wfr 43 in
    # this session hit that exact failure; this scheduler tick
    # prevents future occurrences.
    from services.docker_disk_monitor import run_disk_monitor_tick
    def _disk_monitor_tick():
        try:
            result = run_disk_monitor_tick()
            if result.get("actions") or result.get("alert"):
                logger.info("docker_disk_monitor tick: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("docker_disk_monitor tick raised: %s", e)
    _scheduler.add_job(
        _disk_monitor_tick,
        IntervalTrigger(minutes=15),
        id="docker_disk_monitor",
        name="Docker disk pressure monitor + auto-cleanup",
        replace_existing=True,
    )

    # --- δ DAG-wizard B7 — build-farm tick (every 5 min, gated by
    # FARM_ENABLED env var). Default disabled; activate by setting
    # FARM_ENABLED=1 + restarting the API. Internal back-pressure
    # checks (queue depth, disk, Harbor) skip the tick when the
    # system is busy.
    # GT.P2.3 — build_farm is paid (CE-denied); skip the job when absent.
    if _module_available("services.build_farm"):
        _carve_m = optional_paid("services.build_farm")
        if _carve_m is None:
            return
        run_farm_tick = getattr(_carve_m, "run_farm_tick")
        _scheduler.add_job(
            run_farm_tick,
            IntervalTrigger(minutes=int(os.getenv("FARM_INTERVAL_MINUTES", "5"))),
            id="build_farm_tick",
            name="δ DAG build-farm continuous canary (B7)",
            replace_existing=True,
        )

    # --- Calibration coordinator tick. Drips work into the queue at
    # a bounded rate (target_in_flight on the active plan); see
    # services/calibration_coordinator.py. Cheap no-op when no plan
    # is running. Frequency configurable via
    # CALIBRATION_TICK_SECONDS (default 30s).
    #
    # δ §20.3 — exception observability + UI-honest failure surface.
    # APScheduler's default for a raising job is "log + retry next
    # interval" — silent enough that a recurring failure can leave
    # the plan row's last_tick_at stale for hours without an
    # operator-visible signal (Tim hit this 2026-05-09 04:37–14:42).
    # Wrap the tick so:
    #   1. Any exception lands at ERROR with a full traceback (so
    #      grepping the API log makes the failure obvious).
    #   2. We open a fresh session and stamp last_tick_at +
    #      last_tick_reason='errored: <type>: <truncated msg>' on
    #      the running plan so the Node Training UI shows the
    #      failure rather than the previous tick's stale reason.
    def _calibration_tick():
        from database import SessionLocal
        from services.calibration_coordinator import tick
        s = SessionLocal()
        try:
            tick(s)
        except Exception as e:  # noqa: BLE001
            logger.exception("calibration_coordinator tick raised: %s", e)
            # Best-effort: surface the failure on the plan row so the
            # UI doesn't lie about state. Use a fresh session because
            # the original may be in a pending-rollback state.
            try:
                s.close()
            except Exception:
                pass
            s2 = SessionLocal()
            try:
                from datetime import datetime, timezone
                from models import CalibrationPlan
                plan = s2.query(CalibrationPlan).filter(
                    CalibrationPlan.status == "running",
                ).first()
                if plan is not None:
                    plan.last_tick_at = datetime.now(timezone.utc)
                    plan.last_tick_reason = (
                        f"errored: {type(e).__name__}: {str(e)[:160]}"
                    )
                    s2.commit()
            except Exception as e2:  # noqa: BLE001
                logger.warning(
                    "failed to surface calibration tick error on plan row: %s", e2,
                )
                try:
                    s2.rollback()
                except Exception:
                    pass
            finally:
                s2.close()
            return  # don't re-raise; APScheduler keeps the job scheduled
        finally:
            try:
                s.close()
            except Exception:
                pass

    _scheduler.add_job(
        _calibration_tick,
        IntervalTrigger(seconds=int(os.getenv("CALIBRATION_TICK_SECONDS", "30"))),
        id="calibration_coordinator_tick",
        name="Calibration coordinator (in-flight-bounded dispatch)",
        replace_existing=True,
    )

    # --- Worker maintenance (every 30s) ---
    # Tightened from 5 min to 30s so expired claims (worker hung,
    # network-partitioned, or otherwise silent) get re-queued fast
    # enough that an interactive wizard run notices the failure
    # within the same UI session. The cleanup query is cheap (a
    # status + claim_expires_at index hit on api_task_queue) and
    # only acts on rows whose claim_expires_at is already past;
    # workers with healthy heartbeats keep extending claims so this
    # doesn't kill legitimate long-running builds. Configurable via
    # MOD_WORKER_MAINTENANCE_SECONDS for ops who want to dial back.
    _wm_seconds = int(os.getenv("MOD_WORKER_MAINTENANCE_SECONDS", "30"))
    _scheduler.add_job(
        _run_worker_maintenance,
        IntervalTrigger(seconds=_wm_seconds),
        id="worker_maintenance",
        name="Worker maintenance (cleanup stale tasks)",
        replace_existing=True,
    )

    # --- Worker stall watchdog (every 60s) --- WS4.Q21
    # Slower than worker_maintenance (30s): this only reclassifies status and
    # nudges stuck claims into worker_maintenance's own next tick, so there is
    # no correctness reason to run it as tight — a green-but-silent worker's
    # fetch_loop_age_s takes minutes to cross the default 600s threshold in
    # the first place. Configurable via MOD_WORKER_STALL_WATCHDOG_SECONDS.
    _wsw_seconds = int(os.getenv("MOD_WORKER_STALL_WATCHDOG_SECONDS", "60"))
    _scheduler.add_job(
        _run_worker_stall_watchdog,
        IntervalTrigger(seconds=_wsw_seconds),
        id="worker_stall_watchdog",
        name="Worker stall watchdog (green-but-silent detection)",
        replace_existing=True,
    )

    # --- Host-exec workspace auto-provision (every 30 min) --- hx.14
    # Keeps each eligible worker's host-exec workspace (git clone/fetch of
    # the configured `worker.host_exec_default_repo`) refreshed without
    # anyone having to invoke `mod_exec.py ... workspace sync` by hand.
    # Hardcoded interval (no new env var — this project's tunables are DB
    # platform settings, not os.getenv knobs); the sweep itself is inert
    # (host_exec_enabled off, or no default_repo configured) until an admin
    # opts in via those settings.
    if _module_available("services.worker_host_exec_provision"):
        _scheduler.add_job(
            _run_host_exec_workspace_provision,
            IntervalTrigger(minutes=30),
            id="host_exec_workspace_provision",
            name="Host-exec workspace auto-provision (hx.14)",
            replace_existing=True,
        )
    else:
        logger.info("host_exec_workspace_provision skipped (services.worker_host_exec_provision not in this edition)")

    # --- System-task maintenance (every 2 min) ---
    # SYSTEM-queue tasks run inside the API, not the Go worker. Reap orphaned
    # RUNNING rows AND drain stranded PENDING rows — the latter have no other
    # consumer, so without this a PENDING system task sits forever (and its
    # work, e.g. node-metadata scan, never runs → dependent workflows fail).
    _stm_seconds = int(os.getenv("MOD_SYSTEM_TASK_MAINT_SECONDS", "120"))
    _scheduler.add_job(
        _run_system_task_maintenance,
        IntervalTrigger(seconds=_stm_seconds),
        id="system_task_maintenance",
        name="System-task maintenance (reap RUNNING + drain PENDING)",
        replace_existing=True,
    )

    # --- FX.90 p3a (Q76=A) — async malware scan of large uploads (every min)
    # Large uploads are stored scan-pending + queued as SYSTEM rows by the
    # upload paths; NOTHING else drains those (the Go worker ignores SYSTEM),
    # so this is their consumer. Runs the due rows through
    # services/malware_scan_async.drain_due_scans, which owns the backoff
    # (a down clamd is probed on 120/240/480s, not every tick).
    if _module_available("services.malware_scan_async"):
        _scheduler.add_job(
            _run_malware_scan_drain,
            IntervalTrigger(seconds=60),
            id="malware_scan_async_drain",
            name="Async malware scan drain (large-media verdicts)",
            replace_existing=True,
        )
    else:
        logger.info("malware_scan_async_drain skipped (services.malware_scan_async not in this edition)")

    # --- Scheduler model baseline watchdog (every 10 min) ---
    # If object storage serves no LOADABLE scheduler ONNX (every version is a
    # truncated/corrupt stub), restore the baked-in good baseline so the ML
    # scheduler self-heals instead of falling back to the heuristic forever.
    _mbw_seconds = int(os.getenv("MOD_MODEL_BASELINE_WATCHDOG_SECONDS", "600"))
    _scheduler.add_job(
        _run_model_baseline_watchdog,
        IntervalTrigger(seconds=_mbw_seconds),
        id="model_baseline_watchdog",
        name="Scheduler model baseline watchdog (self-heal corrupt model)",
        replace_existing=True,
    )

    # --- Concurrent-processes counter self-heal (977d, every 10 min) ---
    # utils.usage_enforcement.increment/decrement_concurrent_processes can
    # drift out of sync with the actual in-flight (QUEUED+RUNNING,
    # non-capacity-queued) WorkflowRun population on a missed decrement
    # call-site. This periodically calls the same audited reconcile
    # function (977c) that the one-off owner-triggered pass uses.
    _cpr_seconds = _psr.get_setting(
        "usage.concurrent_processes_reconcile_interval_seconds")
    _scheduler.add_job(
        _run_concurrent_processes_reconcile,
        IntervalTrigger(seconds=_cpr_seconds),
        id="concurrent_processes_reconcile",
        name="Concurrent-processes counter self-heal (tenant drift correction)",
        replace_existing=True,
    )

    # --- Retention cleanup (daily 3am UTC) ---
    _scheduler.add_job(
        _run_retention_cleanup,
        CronTrigger(hour=3, minute=0),
        id="retention_cleanup",
        name="Retention cleanup (delete expired outputs)",
        replace_existing=True,
    )

    # --- Group Q.4 (25.6) — byte-purge retention, ISO-off installs only ---
    # SaaS and any on-prem install that has NOT set MOD_ISO_MODE=false stay
    # ISO 27001 (soft-delete only, never purged); this job is a no-op there
    # (see services.output_retention.apply_retention's iso_mode_on() check).
    _scheduler.add_job(
        _run_output_byte_retention,
        CronTrigger(hour=3, minute=5),
        id="output_byte_retention",
        name="Output byte-purge retention (ISO-off on-prem only)",
        replace_existing=True,
    )

    # --- GY.C5.s2 (25.6) — audit retention GC tick, same nightly slot family ---
    # services.output_retention.audit_gc_tick reads the profile-driven effective
    # audit window (audit.online_retention_months raised to the declared
    # environment's floor) and records the pass as append-only AuditEvents.
    # Gated by the platform setting retention.audit_gc_enabled (default off →
    # the tick is a no-op). Deletes NOTHING (ISO 27001).
    _scheduler.add_job(
        _run_audit_gc,
        CronTrigger(hour=3, minute=10),
        id="audit_gc",
        name="Audit retention GC tick (profile window, append-only)",
        replace_existing=True,
    )

    # --- GX.R4 (launch-completion-megaplan Group X) — ephemeral run inputs ---
    # Stage 1 soft-expires rows past TTL; stage 2 purges the object bytes after
    # the grace period (all modes). Rows are NEVER
    # deleted (ISO 27001). Every 30 min so expiry is timely for the 410-on-run
    # check and grace turnover.
    _scheduler.add_job(
        _run_ephemeral_input_retention,
        IntervalTrigger(minutes=30),
        id="ephemeral_input_retention",
        name="Ephemeral run-input retention (expire + byte purge)",
        replace_existing=True,
    )

    # Self-heal missing tenant-{id}-files buckets (best-effort provisioning at
    # tenant creation / storage-backend switches left some tenants without
    # one → presigned uploads 404 NoSuchBucket). Runs shortly after boot too.
    from apscheduler.triggers.interval import IntervalTrigger as _BucketInterval
    _scheduler.add_job(
        _run_tenant_bucket_reconcile,
        _BucketInterval(hours=6),
        id="tenant_bucket_reconcile",
        name="Recreate missing tenant file buckets",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        replace_existing=True,
    )

    # --- workflow-agentic-execution-loop §wizard-test-run-dispatch ---
    # Daily sweep of expired wizard sandbox test-run prefixes. Removes
    # the `_wizard_test_runs/{session_id}/` MinIO prefix when the latest
    # iteration for that session is past the configured TTL (default
    # 24h). Best-effort — failures log but don't crash the scheduler.
    if _module_available("services.wizard_test_run_cleanup"):
        _scheduler.add_job(
            _run_wizard_test_run_sweep,
            CronTrigger(hour=3, minute=15),
            id="wizard_test_run_sweep",
            name="Wizard sandbox test-run prefix sweep",
            replace_existing=True,
        )

    # --- workflow-agentic-execution-loop §workflow-node-output-cache ---
    # Daily sweep of stale node_output_cache rows. TTL default 90 days
    # against last_accessed_at — hot rows stay because every cache hit
    # updates the timestamp. Best-effort.
    _scheduler.add_job(
        _run_node_output_cache_sweep,
        CronTrigger(hour=3, minute=30),
        id="node_output_cache_sweep",
        name="Node output cache TTL sweep",
        replace_existing=True,
    )

    # --- workflow-agentic-execution-loop §quality-signal ---
    # Daily detector: scan recent successful applies, compare each
    # workflow's current graph against the graph at apply time, insert
    # a `downstream_edited` signal where they differ. Window 7 days.
    # Best-effort.
    _scheduler.add_job(
        _run_wizard_downstream_edit_detector,
        CronTrigger(hour=3, minute=45),
        id="wizard_downstream_edit_detector",
        name="Wizard downstream-edit detector",
        replace_existing=True,
    )

    # --- Tenant retirement (daily 4am UTC, right after retention cleanup) ---
    # Hard-deletes tenants whose hard_delete_after date has passed:
    # drops the Harbor project + MinIO bucket + Vault secrets + DB row.
    # Default 90-day grace period from archive → retire (configurable
    # per-tenant via retention_days). ISO 27001 AU-11 / SC-28.
    if _module_available("utils.tenant_harbor_client"):
        _scheduler.add_job(
            _run_retire_archived_tenants,
            CronTrigger(hour=4, minute=0),
            id="retire_archived_tenants",
            name="Retire archived tenants past retention",
            replace_existing=True,
        )
    else:
        logger.info("retire_archived_tenants skipped (utils.tenant_harbor_client not in this edition)")

    # --- FX.15 (GSEC R-015/R-016) — privacy erasure (daily 05:30 UTC) ---
    # Destroys the object-store bytes + the tenant encryption key of
    # users/tenants soft-deleted at least privacy.erasure_grace_days ago
    # (platform setting, default 30). Rows are NEVER deleted (ISO 27001);
    # runs in ISO mode too — it is the only purge path there. BYO-S3
    # tenants are report-only (customer buckets never touched).
    _scheduler.add_job(
        _run_privacy_erasure,
        CronTrigger(hour=5, minute=30),
        id="privacy_erasure",
        name="FX.15 — privacy erasure (bytes + keys; rows stay)",
        replace_existing=True,
    )

    # --- FX.59 p5a — day-60 tenant-key crypto purge (daily 04:35 UTC) ---
    # Tenants whose deletion was requested at least
    # tenant.deletion_purge_after_days ago (default 60): destroy the
    # tenant's encryption key (per-tenant Vault transit key + DB-side
    # copy), stamp the audit row, write ONE append-only deletion
    # attestation. Rows are NEVER deleted (ISO 27001). Idempotent via
    # the attestation. DRY mode (report only, nothing destroyed) while
    # tenant.crypto_purge_enabled is False (default).
    # 1788l CE carve: the purge's paid modules (services/deletion_attestation.py +
    # services/key_provider.py) are denied in CE, so the job is never registered — one
    # clean "not in this edition" line at startup instead of an ImportError every tick.
    if _module_available("services.deletion_attestation") and _module_available(
            "services.key_provider"
    ):
        _scheduler.add_job(
            _run_tenant_key_shred,
            CronTrigger(hour=4, minute=35),
            id="tenant_key_purge",
            name="FX.59 p5a — day-60 tenant-key crypto purge",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info(
            "scheduler: tenant_key_purge job not registered — "
            "services.deletion_attestation/services.key_provider not in this edition"
        )

    # --- FX.40 part 1 (owner decision Q22=A) — scheduler-data pseudonymisation
    # (daily 06:40 UTC). Stamps api_task_queue rows older than
    # scheduler.pseudonymise_after_days (default 90) with tenant_pseudonym =
    # HMAC-SHA256(Vault key infrastructure/scheduler/pseudonym_key, tenant_id)
    # truncated to 32 hex chars ("p_" prefix) + pseudonymised_at, so the
    # scheduler retrain corpus (api_task_queue joined with task_resource_samples
    # / prediction_logs) can be keyed by pseudonym instead of tenant_id. No
    # rows are deleted and tenant_id is never nulled (nullable=False — part-2
    # blocker). Off unless scheduler.pseudonymise_job_enabled is True; with no
    # Vault key the sweep logs a WARNING and changes nothing.
    if _module_available("services.scheduler_pseudonymise"):
        _scheduler.add_job(
            _run_scheduler_pseudonymise,
            CronTrigger(hour=6, minute=40),
            id="scheduler_pseudonymise",
            name="FX.40 — scheduler-data pseudonymisation (rows stay)",
            replace_existing=True,
        )
    else:
        logger.info("scheduler_pseudonymise skipped (services.scheduler_pseudonymise not in this edition)")

    # --- FX.40 part 2 (owner decision Q22=A) — scheduler-data aggregate
    # roll-up (daily 06:50 UTC, after the 06:40 pseudonymise sweep). Folds
    # api_task_queue rows older than scheduler.aggregate_after_months
    # (default 24 — the Q22 retention window after the 90-day pseudonymise)
    # into scheduler_training_aggregates (tenant-free: node_kind / queue /
    # gpu_model / month / success buckets with count + duration p50/p95 +
    # peak-resource stats — exactly what the retrain reads) and soft-marks
    # the source rows aggregated_at. No rows are deleted and tenant_id is
    # never touched (nullable=False — out of scope per Q22 part 2). Off
    # unless scheduler.aggregate_job_enabled is True.
    if _module_available("services.scheduler_pseudonymise"):
        _scheduler.add_job(
            _run_scheduler_aggregate,
            CronTrigger(hour=6, minute=50),
            id="scheduler_aggregate",
            name="FX.40 — scheduler-data aggregate roll-up (rows stay)",
            replace_existing=True,
        )
    else:
        logger.info("scheduler_aggregate skipped (services.scheduler_pseudonymise not in this edition)")

    # --- GY.V8 (BIPA) — biometric scheduled destruction (daily 05:40 UTC) ---
    # Soft-deletes biometric-labelled outputs past their consent retention
    # deadline (stated period, or 3 years from last interaction, whichever
    # first) via the existing soft-delete erasure path, skipping legal
    # holds. Rows are NEVER hard-deleted (ISO 27001). Off unless the
    # privacy.biometric_destruction_job_enabled platform setting is True.
    _scheduler.add_job(
        _run_biometric_destruction,
        CronTrigger(hour=5, minute=40),
        id="biometric_destruction",
        name="GY.V8 — biometric (BIPA) scheduled destruction (soft-delete; "
             "holds skipped)",
        replace_existing=True,
    )

    # --- Q17 (owner decision, 2026-10-03) — sales-lead PII retention (daily 06:15 UTC) ---
    # Scrubs the personal fields (name/email/message) of sales leads aged past
    # leads.retention_open_inactive_months (open) or leads.retention_closed_months
    # (closed), stamping pii_scrubbed_at. The row itself stays (ISO 27001 — no
    # hard deletes) so sales stats keep working. Converted (customer) and
    # legal-held leads are always skipped; a hold-lookup error fails closed.
    # Off unless leads.retention_job_enabled is True.
    _scheduler.add_job(
        leads_retention_tick,
        CronTrigger(hour=6, minute=15),
        id="leads_retention",
        name="Q17 — sales-lead PII retention scrub (rows stay)",
        replace_existing=True,
    )

    # --- GSEC FX.39 (owner decision Q21=A) — billing-record retention (nightly 03:20 UTC) ---
    # Purges the PERSONAL fields (name/email/address/tax id/contact, free-text
    # PII) of invoice + payment + tax rows older than
    # billing.record_retention_years (default 10 years) through the FX.15
    # crypto-purge path. Rows and financial figures always stay, anonymised
    # (ISO 27001 — no hard deletes); each erasure is recorded in the
    # restore-safety ledger and one audit row is written per batch. Legal-held
    # tenants are skipped and a hold-lookup error fails closed. Off unless
    # billing.retention_job_enabled is True.
    _scheduler.add_job(
        billing_retention_tick,
        CronTrigger(hour=3, minute=20),
        id="billing_retention",
        name="FX.39 — billing-record retention purge (rows + figures stay)",
        replace_existing=True,
    )

    # --- GSEC FX.92 (owner decision Q78=A) — hosted cancellation lifecycle (daily 04:10 UTC) ---
    # For every SaaS tenant in a post-cancellation read-only export window:
    # send due reminder emails (day 0, 7, N-7, N-1 — idempotent per label)
    # and, once the window has ended, soft-delete the tenant (ISO 27001 —
    # the row + audit rows stay) and trigger the FX.15 byte erasure for it.
    # Legal hold blocks the deletion and a hold-lookup error fails closed.
    # On-prem never runs. Off unless
    # billing.cancellation_lifecycle_enabled is True.
    _scheduler.add_job(
        cancellation_lifecycle_tick,
        CronTrigger(hour=4, minute=10),
        id="cancellation_lifecycle",
        name="FX.92 — hosted cancellation export window (reminders + "
             "end-of-window soft delete + erasure; holds block)",
        replace_existing=True,
    )

    # --- GSEC FX.126 (Q113=A): asset-inventory outdated-worker alert (daily 04:40 UTC) ---
    # Flags workers running an agent version older than the current release
    # manifest and emits one audit row per outdated worker (rate-limited to
    # ~once/day per worker). Off unless assets.outdated_worker_alert_enabled
    # is True (default on). services.asset_inventory.run_outdated_worker_
    # alert_tick owns the comparison + the audit writes.
    if _module_available("services.asset_inventory"):
        _scheduler.add_job(
            asset_outdated_worker_alert_tick,
            CronTrigger(hour=4, minute=40),
            id="asset_outdated_worker_alert",
            name="Asset inventory — outdated worker version alert (GSEC FX.126)",
            replace_existing=True,
        )
    else:
        logger.info("asset_outdated_worker_alert skipped (services.asset_inventory not in this edition)")

    # --- GSEC FX.153 part 2 (owner decision Q140=B) — staff-register due email
    # (daily 04:55 UTC, right after the FX.126 04:40 alert) ---
    # Emails every ACTIVE platform admin the current GET /platform/staff-
    # records/due list: staff whose disciplinary training is unrecorded/older
    # than staff.training_interval_days, and leavers whose 7-system access-
    # removal checklist is still incomplete past staff.leaver_access_removal_hours
    # (the policy's "all access removed within 24 hours"). Nothing is sent when
    # nothing is due. Off unless staff.due_email_enabled is True (default on).
    if _module_available("services.staff_register"):
        _scheduler.add_job(
            staff_register_due_email_tick,
            CronTrigger(hour=4, minute=55),
            id="staff_register_due_email",
            name="GSEC FX.153 — staff register due email (training + leaver checklist)",
            replace_existing=True,
        )
    else:
        logger.info("staff_register_due_email skipped (services.staff_register not in this edition)")

    # --- GSEC FX.125 (Q112=A): platform key-rotation overdue sweep (daily 04:52 UTC) ---
    # Builds the runtime key inventory (services/key_inventory.py), ages
    # each item against the keys.* rotation-schedule settings, and while
    # keys.rotation_alert_enabled is True writes one admin Notification
    # + one KEY_ROTATION_OVERDUE audit row per overdue item per day
    # (deduped via the notification dedupe_key). Off unless
    # keys.rotation_alert_enabled is True (default on). Reporting only —
    # part 1 rotates nothing; the rotation jobs are part 2.
    def _fx125_key_rotation_overdue_tick():
        db = _get_db()
        try:
            from services import key_inventory as _fx125
            summary = _fx125.run_rotation_overdue_sweep(db)
            logger.info("fx125 key-rotation sweep: %s", summary)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx125 key-rotation sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.key_inventory"):
        _scheduler.add_job(
            _fx125_key_rotation_overdue_tick,
            CronTrigger(hour=4, minute=52),  # 04:52 UTC daily
            id="fx125_key_rotation_overdue",
            name="FX.125 platform key rotation overdue sweep (GSEC Q112=A)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx125_key_rotation_overdue skipped (services.key_inventory not in this edition)")

    # --- GSEC FX.125 p2a (Q112=A): 90-day internal-TLS cert renewal (hourly) ---
    # The overdue sweep above only FLAGS certs past keys.cert_rotation_days (90);
    # this is the half that closes the gap: services/cert_rotation.py decides due-
    # ness (a leaf past the 90d bound, or expiring inside tls.cert_expiry_warn_days)
    # and invokes the install's OWN idempotent renewal — CORE/startup/lib/
    # internal-certs.sh renew re-signs exactly the due leaves against the persisted
    # CA anchor, so nothing else has to re-trust anything. INERT BY DEFAULT:
    # keys.cert_rotation_enabled is False, so the tick plans nothing and writes
    # nothing until an operator turns it on; keys.cert_rotation_interval_days
    # (default 7) bounds how late the 90d bound may be enforced, and the hourly
    # cadence bounds lateness of that interval. Same seam gap as the restore-test
    # and backup sweeps: the API has no host-exec channel, so the tick PLANS the
    # command; a host-exec seam would pass it to run_cert_renewal(runner=...).
    # Recording: one SYSTEM ApiTaskQueue row per attempt + one AuditAction.KEY_ROTATED
    # row per cert actually rotated (detected by diffing the cert windows, metadata
    # only). Nothing is deleted.
    def _fx125p2a_cert_renewal_tick():
        db = _get_db()
        try:
            from services.cert_rotation import sweep_once
            result = sweep_once(db)
            if result.get("planned"):
                logger.info(
                    "[fx125p2a_cert_renewal] planned %s (bound=%s days, interval=%s days)",
                    " ".join(result["command"]), result["rotation_days"],
                    result["interval_days"],
                )
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx125p2a cert-renewal tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.cert_rotation"):
        _scheduler.add_job(
            _fx125p2a_cert_renewal_tick,
            IntervalTrigger(hours=1),
            id="fx125p2a_cert_renewal",
            name="FX.125 p2a 90-day internal-TLS cert renewal (GSEC Q112=A)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx125p2a_cert_renewal skipped (services.cert_rotation not in this edition)")

    # --- GSEC FX.125 p2c (Q112=A): 30-day enrollment-token rotation (hourly) ---
    # The overdue sweep above only FLAGS the enrollment pool past keys.enrollment_token_rotation_days
    # (30); this is the half that closes the gap: services/enroll_token_rotation.py
    # soft-revokes (revoked_at, never a delete) every worker/manager enrollment token
    # whose AGE passed that bound OR whose own expires_at already passed — the
    # expiry was always enforced only at redemption, so an expired token still sat
    # un-revoked in the pool the inventory and the admin pages read. INERT BY
    # DEFAULT: keys.enrollment_token_rotation_enabled is False, so the tick plans
    # nothing and writes nothing until an operator turns it on; keys.enrollment_token_rotation_interval_days
    # (default 1) bounds how late the 30d bound may be enforced, and the hourly
    # cadence bounds lateness of that interval. Already-enrolled workers, their
    # WorkerEnrollment rows and their LICENCES are never touched — revoking a token
    # gates future enrollments only. Recording: one SYSTEM ApiTaskQueue row per
    # attempt + one AuditAction.KEY_ROTATED row per token actually revoked
    # (re-read after the write, metadata only, never a token hash).
    def _fx125p2c_enroll_token_rotation_tick():
        db = _get_db()
        try:
            from services.enroll_token_rotation import sweep_once as _p2c_sweep
            result = _p2c_sweep(db)
            if result.get("planned"):
                logger.info(
                    "[fx125p2c_enroll_token_rotation] would revoke %s "
                    "(bound=%s days, interval=%s days)",
                    " ".join(result["due"]), result["rotation_days"],
                    result["interval_days"],
                )
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx125p2c enroll-token-rotation tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.enroll_token_rotation"):
        _scheduler.add_job(
            _fx125p2c_enroll_token_rotation_tick,
            IntervalTrigger(hours=1),
            id="fx125p2c_enroll_token_rotation",
            name="FX.125 p2c 30-day enrollment-token rotation (GSEC Q112=A)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx125p2c_enroll_token_rotation skipped "
                    "(services.enroll_token_rotation not in this edition)")

    # --- 30.6.8 c8-A2 — stack identity in-place rotation (hourly) ---
    # services/stack_identity.py:run_stack_identity_rotation re-mints and pushes
    # via the worker's refresh_secret_files op at 50% TTL (dir-bind stacks on
    # secret_refresh workers only); stacks that cannot push get ONE deduped
    # Notification under 25% remaining — never an auto-redeploy (BUG-1027h).
    def _c8a2_stack_identity_rotation_tick():
        db = _get_db()
        try:
            from services.stack_identity import run_stack_identity_rotation as _c8a2_run
            result = _c8a2_run(db)
            if result.get("pushed") or result.get("notified"):
                logger.info("[c8a2_stack_identity_rotation] checked=%s pushed=%s "
                            "redeploy_required=%s skipped=%s notified=%s",
                            result["checked"], result["pushed"],
                            result["redeploy_required"], result["skipped"],
                            result["notified"])
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("c8a2 stack-identity-rotation tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.stack_identity"):
        _scheduler.add_job(
            _c8a2_stack_identity_rotation_tick,
            IntervalTrigger(hours=1),
            id="c8a2_stack_identity_rotation",
            name="30.6.8 c8-A2 stack identity rotation (hourly, 50% TTL)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("c8a2_stack_identity_rotation skipped "
                    "(services.stack_identity not in this edition)")

    # --- GSEC FX.125 p2b — bootstrap DB-password re-roll (hourly) ---
    # The overdue sweep above only FLAGS the bootstrap DB password past keys.db_password_rotation_days
    # (90); this is the half that closes the gap: services/db_password_rotation.py ages the
    # KV v2 `infrastructure/postgres` entry (vault-init.sh writes it, render-env-from-vault.sh
    # reads it back into .env.secrets, compose interpolates it into postgres-app's POSTGRES_PASSWORD)
    # against that bound using Vault's OWN content_info.created_time — the creation timestamp
    # part 1 said was missing, which is why its inventory item could only read "unknown".
    # INERT BY DEFAULT: keys.db_password_rotation_enabled is False, so the tick plans nothing
    # and writes nothing until an operator turns it on; keys.db_password_rotation_interval_days
    # (default 7) bounds how late the 90d bound may be enforced, and the hourly cadence bounds
    # lateness of that interval. THE SEAM: re-rolling the bootstrap password is not API-only —
    # the new value must also be re-rendered into .env.secrets and postgres-app recreated
    # (compose interpolates it at container-CREATE), so with no ops seam injected the job PLANS
    # and applies nothing. The DYNAMIC api-readwrite role is NEVER touched here — it already
    # self-rotates at 50% of its 1h lease TTL in database.py's renewal loop. Recording: one
    # SYSTEM ApiTaskQueue row per attempt + one AuditAction.KEY_ROTATED row per password
    # actually re-rolled (detected by diffing the KV entry's Vault version before/after,
    # metadata only — never a password, never a hash).
    def _fx125p2b_db_password_rotation_tick():
        db = _get_db()
        try:
            from services.db_password_rotation import sweep_once as _p2b_sweep
            result = _p2b_sweep(db)
            if result.get("planned"):
                logger.info(
                    "[fx125p2b_db_password_rotation] would re-roll %s "
                    "(bound=%s days, interval=%s days, kv=%s)",
                    " ".join(result["due"]), result["rotation_days"],
                    result["interval_days"], result.get("kv", {}).get("path"),
                )
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx125p2b db-password-rotation tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.db_password_rotation"):
        _scheduler.add_job(
            _fx125p2b_db_password_rotation_tick,
            IntervalTrigger(hours=1),
            id="fx125p2b_db_password_rotation",
            name="FX.125 p2b 90-day bootstrap DB-password re-roll (GSEC Q112=A)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx125p2b_db_password_rotation skipped "
                    "(services.db_password_rotation not in this edition)")

    # --- Wizard-rebuild §11b — retention hard-delete sweep (daily 02:00 UTC) ---
    # Hard-deletes wizard-managed rows (NodeDefinition, BuildSpecRecord,
    # WorkflowRun, NodeVariantVerification, RepoDocsExtract) whose
    # ``deleted_at`` is older than the tenant's ``retention_days``
    # (default 90, bounded 30-2555 per ISO 27001 A.8.2.3 + 18.1.3).
    # For NodeDefinition rows the Harbor manifest is reclaimed first.
    # Each hard-deletion emits a CONTENT_RETENTION_EXPIRED audit event.
    _scheduler.add_job(
        _run_hard_delete_after_retention,
        CronTrigger(hour=2, minute=0),
        id="hard_delete_after_retention",
        name="§11b — hard-delete soft-deleted rows past tenant retention",
        replace_existing=True,
    )

    # --- Harbor quota alerts (hourly) ---
    # Check every active tenant's Harbor storage; email + audit at 80%,
    # set quota_push_blocked at 95%. Idempotent via harbor_quota_alerts
    # (one row per tenant+threshold+24h window).
    if _module_available("utils.tenant_harbor_client"):
        _scheduler.add_job(
            _run_check_harbor_quotas,
            IntervalTrigger(hours=1),
            id="check_harbor_quotas",
            name="Harbor quota alerts (80% warn / 95% block)",
            replace_existing=True,
        )
    else:
        logger.info("check_harbor_quotas skipped (utils.tenant_harbor_client not in this edition)")

    # --- Harbor usage snapshots (daily 2am UTC) ---
    # Records every tenant's Harbor used_bytes into
    # tenant_harbor_usage_daily for monthly billing reconciliation.
    if _module_available("utils.tenant_harbor_client"):
        _scheduler.add_job(
            _run_snapshot_harbor_usage,
            CronTrigger(hour=2, minute=0),
            id="snapshot_harbor_usage",
            name="Harbor usage snapshot (billing reconciliation)",
            replace_existing=True,
        )
    else:
        logger.info("snapshot_harbor_usage skipped (utils.tenant_harbor_client not in this edition)")

    # --- Storage metrics (daily 6am UTC) ---
    _scheduler.add_job(
        _run_storage_metrics,
        CronTrigger(hour=6, minute=0),
        id="storage_metrics",
        name="Storage metrics (update tenant usage)",
        replace_existing=True,
    )

    # --- Repo-docs extract sweep (hourly) ---
    # Deletes wizard-rebuild §3c cache rows whose 7-day TTL has
    # expired. Idempotent — running too often costs a no-op DELETE.
    _scheduler.add_job(
        _run_repo_docs_extract_sweep,
        IntervalTrigger(hours=1),
        id="repo_docs_extract_sweep",
        name="Wizard repo-docs cache sweep (delete expired)",
        replace_existing=True,
    )

    # --- Variant-test pending sweep (hourly) ---
    # wizard-rebuild §9c — expires pending sample-tests whose
    # 30-day TTL elapsed without matching hardware ever joining
    # the cluster. Expired rows flip the variant's verification
    # verdict to `untested_awaiting_hardware`.
    _scheduler.add_job(
        _run_variant_test_pending_sweep,
        IntervalTrigger(hours=1),
        id="variant_test_pending_sweep",
        name="Wizard variant-test pending sweep (expire stale)",
        replace_existing=True,
    )

    # --- Variant-test backfill (every 20 min) ---
    # Re-test / re-build accelerator variants a registered node is missing but
    # whose hardware just came online — the counterpart to the expire sweep
    # above (which only ages rows out). Capped build fan-out per tick.
    _scheduler.add_job(
        _run_variant_backfill_sweep,
        IntervalTrigger(minutes=20),
        id="variant_backfill_sweep",
        name="Variant-test backfill (re-test/re-build missing variants)",
        replace_existing=True,
    )

    # --- Permission expiration check (daily 1am UTC) ---
    _scheduler.add_job(
        _run_permission_expiration_check,
        CronTrigger(hour=1, minute=0),
        id="permission_expiration",
        name="Permission expiration check",
        replace_existing=True,
    )

    # --- Credit renewal (daily 2am UTC) ---
    if _module_available("utils.credit_renewal"):
        _scheduler.add_job(
            _run_credit_renewal,
            CronTrigger(hour=2, minute=0),
            id="credit_renewal",
            name="Credit renewal (monthly subscriptions)",
            replace_existing=True,
        )
    else:
        logger.info("credit_renewal skipped (utils.credit_renewal not in this edition)")

    # --- Grace period check (daily 4am UTC) ---
    if _module_available("utils.grace_period"):
        _scheduler.add_job(
            _run_grace_period_check,
            CronTrigger(hour=4, minute=0),
            id="grace_period_check",
            name="Grace period check (suspend overdue tenants)",
            replace_existing=True,
        )
    else:
        logger.info("grace_period_check skipped (utils.grace_period not in this edition)")

    # --- GY.C4c — inactive-account auto-disable (AC-2(3)), daily 05:15 UTC ---
    # services.inactive_accounts.run_inactive_disable: every account whose last
    # recorded activity (user_sessions.last_active_at, else users.created_at) is
    # older than `auth.inactive_disable_days` is disabled — Keycloak
    # enabled=false + status=DEACTIVATED, never a delete (ISO 27001). The
    # setting is re-read every tick, so 0 disables the job without a restart;
    # the trigger itself is a fixed daily heartbeat. Exempt emails and service
    # accounts are always skipped; platform admins are not.
    def _inactive_account_disable_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.inactive_accounts")
        if _carve_m is None:
            return
        run_inactive_disable = getattr(_carve_m, "run_inactive_disable")
        try:
            db = _SL()
            try:
                summary = run_inactive_disable(db)
                if summary.get("disabled"):
                    logger.warning(
                        "inactive_account_disable: %d account(s) disabled "
                        "(threshold %d days)",
                        len(summary["disabled"]), summary.get("days", 0),
                    )
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            logger.exception("inactive_account_disable tick raised: %s", e)

    if _module_available("services.inactive_accounts"):
        _scheduler.add_job(
            _inactive_account_disable_tick,
            CronTrigger(hour=5, minute=15),  # 05:15 UTC
            id="inactive_account_disable",
            name="Inactive account auto-disable (AC-2(3))",
            replace_existing=True,
        )
    else:
        logger.info("inactive_account_disable skipped (services.inactive_accounts not in this edition)")

    # --- Usage alerts (daily 7am UTC, after storage metrics at 6am) ---
    if (_module_available("utils.usage_alerts")
            and _module_available("services.downgrade_readonly_service")):
        _scheduler.add_job(
            _run_usage_alerts,
            CronTrigger(hour=7, minute=0),
            id="usage_alerts",
            name="Usage limit alerts (80%/95% credits + storage)",
            replace_existing=True,
        )
    else:
        logger.info("usage_alerts skipped (utils.usage_alerts / services.downgrade_readonly_service not in this edition)")

    # --- Trivy CVE DB rebuild (weekly, Sunday 2am UTC) ---
    _scheduler.add_job(
        _run_trivy_db_rebuild,
        CronTrigger(day_of_week='sun', hour=2, minute=0),
        id="trivy_db_rebuild",
        name="Rebuild TrivyScanner container with fresh CVE database",
        replace_existing=True,
    )

    # --- Payment retry (every 6 hours) ---
    if _module_available("utils.payment_retry"):
        _scheduler.add_job(
            _run_payment_retry,
            IntervalTrigger(hours=6),
            id="payment_retry",
            name="Payment retry (failed invoices)",
            replace_existing=True,
        )
    else:
        logger.info("payment_retry skipped (utils.payment_retry not in this edition)")

    # mod-assistant-agent §5.8 — assistant session expiry sweep.
    # Hourly DELETE of AssistantSession rows whose last_active_at is
    # older than the owning tenant's session_retention_hours (default
    # 1h, configurable in Settings → AI Configuration → Agent up to
    # 168h = 1 week). Cascade deletes the AssistantMessage rows.
    # Single SQL pass; bounded by tenant count, cheap.
    if _module_available("services.assistant_orchestrator"):
        _scheduler.add_job(
            _run_assistant_session_expiry,
            IntervalTrigger(hours=1),
            id="assistant_session_expiry",
            name="MOD Assistant: expire idle chat sessions",
            replace_existing=True,
        )
    else:
        logger.info("assistant_session_expiry skipped (services.assistant_orchestrator not in this edition)")

    # --- Monthly billing (1st of month, 02:00 UTC) ---
    # Fires once per month to roll up unbilled CreditUsageLog rows
    # into Invoice rows for every tenant and charge each tenant's
    # saved payment method via Stripe for postpaid usage + prepaid
    # overage. Without this job, charges never fire — usage
    # accumulates as unbilled forever. Before launch this was a
    # documented service endpoint
    # (/billing/service/process-monthly-billing) intended to be
    # called by APScheduler but never actually wired up. Hour 2
    # chosen so the day-1 run lands after any timezone-spread
    # midnight traffic spike has settled. Idempotent: the processor
    # only touches CreditUsageLog rows where billed_in_invoice_id IS
    # NULL, so a manual re-run for a period that's already invoiced
    # is a no-op.
    if (_module_available("utils.monthly_billing_processor")
            and _module_available("services.billing_alerts")):
        _scheduler.add_job(
            _run_monthly_billing,
            CronTrigger(day=1, hour=2, minute=0),
            id="monthly_billing",
            name="Monthly billing — process invoices and charge",
            replace_existing=True,
        )
    else:
        logger.info("monthly_billing skipped (utils.monthly_billing_processor / services.billing_alerts not in this edition)")

    # --- Weekly overage billing (Mondays 03:00 UTC) ---
    # weekly-overage-billing Slice 2 — collect accrued overage (egress + extra
    # credits) weekly instead of letting a month of it pile up before the
    # monthly invoice. Issues 'overage_interim' invoices for the unbilled delta;
    # the monthly run nets these out so nothing is charged twice. SaaS-only and
    # gated by ENABLE_WEEKLY_OVERAGE_BILLING (default on). Hour 03:00 keeps it
    # clear of the day-1 monthly run at 02:00.
    if _module_available("utils.overage_billing"):
        _scheduler.add_job(
            _run_weekly_overage_billing,
            CronTrigger(day_of_week='mon', hour=3, minute=0),
            id="weekly_overage_billing",
            name="Weekly overage billing — interim overage charges",
            replace_existing=True,
        )
    else:
        logger.info("weekly_overage_billing skipped (utils.overage_billing not in this edition)")

    # --- Weekly support response-time report (Mondays 08:00 UTC) ---
    # GSEC FX.70 part 4 — the weekly support report to platform admins
    # (services/support_ticket_report.weekly_report + send_weekly_report):
    # per severity + plan tier counts opened / responded / within-target /
    # breached / still-open-overdue + median + p90 first-response minutes.
    # 08:00 sits clear of the Monday 03:00 overage run. The send honours the
    # FX.70 on-prem email rule (own-configured email only) and never raises.
    # Gated like weekly_overage_billing: services/support_ticket_report is
    # denied in CE, so CE never registers the job (no weekly ERROR line).
    if _module_available("services.support_ticket_report"):
        _scheduler.add_job(
            _run_support_weekly_report,
            CronTrigger(day_of_week='mon', hour=8, minute=0),
            id="fx70_support_weekly_report",
            name="Weekly support response-time report — platform admins",
            replace_existing=True,
        )
    else:
        logger.info("fx70_support_weekly_report skipped (services.support_ticket_report not in this edition)")

    # --- Prepaid balance mirror reconciliation (daily 03:47 UTC, SaaS-only) ---
    # GB.PAY.12 — `Tenant.prepaid_balance_cents` is only a local mirror of
    # Stripe's Customer Balance; Stripe can apply that balance to a
    # subscription invoice on its own, drifting the mirror without MOD ever
    # calling credit/debit_customer_balance. This daily sweep re-syncs every
    # relevant tenant's mirror against Stripe and audits any drift found
    # (`reconcile_prepaid_mirrors` itself also no-ops on-prem, but the job
    # is only registered at all on SaaS so on-prem never schedules a Stripe
    # round trip in the first place). 03:47 sits clear of the day-1 monthly
    # run (02:00) and the Monday weekly-overage run (03:00), and avoids the
    # top-of-hour stampede other jobs land on.
    if os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas":
        _scheduler.add_job(
            _run_prepaid_reconcile,
            CronTrigger(hour=3, minute=47),
            id="prepaid_reconcile",
            name="Prepaid balance mirror reconciliation",
            replace_existing=True,
        )

    # --- Workflow trigger evaluation (every 1 min) ---
    _scheduler.add_job(
        _run_workflow_triggers,
        IntervalTrigger(minutes=1),
        id="workflow_triggers",
        name="Workflow trigger evaluation (cron schedules)",
        replace_existing=True,
    )

    # --- System-threshold trigger evaluation (every 1 min, add-workflow-triggers §6.4) ---
    _scheduler.add_job(
        _run_system_threshold_triggers,
        IntervalTrigger(minutes=1),
        id="system_threshold_triggers",
        name="Workflow trigger evaluation (system/resource thresholds)",
        replace_existing=True,
    )

    # --- Poll trigger evaluation (every 1 min, add-workflow-triggers §8.3) ---
    _scheduler.add_job(
        _run_poll_triggers,
        IntervalTrigger(minutes=1),
        id="poll_triggers",
        name="Workflow trigger evaluation (HTTP/RSS polling)",
        replace_existing=True,
    )

    # --- Calendar trigger evaluation (every 5 min, EXPR 13.5) ---
    _scheduler.add_job(
        _run_calendar_triggers,
        IntervalTrigger(minutes=5),
        id="calendar_triggers",
        name="Workflow trigger evaluation (calendar polling)",
        replace_existing=True,
    )

    # --- Unrunnable-task sweep (every 5 min) ---
    _scheduler.add_job(
        _run_unrunnable_tasks_sweep,
        IntervalTrigger(minutes=5),
        id="unrunnable_tasks_sweep",
        name="Unrunnable-task sweep (gpu_variants × worker hw_tags)",
        replace_existing=True,
    )

    # --- Compliance withheld-task escalation (every 5 min) ---
    _scheduler.add_job(
        _run_compliance_withheld_sweep,
        IntervalTrigger(minutes=5),
        id="compliance_withheld_sweep",
        name="Compliance withheld-task stuck escalation",
        replace_existing=True,
    )

    # --- Stack orphan sweep (every 5 min) ---
    # Reclaims accelerators held by stacks wedged mid-operation. Committed
    # capacity is derived from stack status, so a stack that never leaves
    # "deploying" strands its card indefinitely.
    _scheduler.add_job(
        _run_stack_orphan_sweep,
        IntervalTrigger(minutes=5),
        id="stack_orphan_sweep",
        name="Stack orphan sweep (release accelerators from wedged stacks)",
        replace_existing=True,
    )

    # Companion to the sweep above: that one handles a stack wedged in a
    # transitional status, this one handles the TASK being undeliverable
    # because its pinned host no longer exists.
    _scheduler.add_job(
        _run_stranded_stack_task_sweep,
        IntervalTrigger(minutes=10),
        id="stranded_stack_task_sweep",
        name="Stranded stack-task sweep (pinned to a decommissioned worker)",
        replace_existing=True,
    )

    # --- LDF-4 §7.3 Concurrency dispatch (every 30s) ---
    # Wakes capacity-queued workflow runs when in-flight slots free up.
    _scheduler.add_job(
        _run_concurrency_dispatch,
        IntervalTrigger(seconds=30),
        id="concurrency_dispatch",
        name="Capacity-queued workflow dispatch (per-tenant, FIFO)",
        replace_existing=True,
    )

    # --- launch-pricing-lockdown §8.4 GPU pool wait-breach alert ---
    # Checks the pool's avg queue wait every minute. When the wait
    # sustains over the threshold (default 30s) for the configured
    # window (default 5 min), fires an internal alert (audit event +
    # logger ERROR + best-effort email to platform admin). Cooldown
    # (default 60 min) prevents email storms during ongoing breaches.
    _scheduler.add_job(
        _run_gpu_pool_alerts,
        IntervalTrigger(minutes=1),
        id="gpu_pool_alerts",
        name="GPU pool queue-wait breach alerts",
        replace_existing=True,
    )

    # --- Mesh/replication metrics refresh + RPO-breach alert (every 30s) ---
    # Repopulates the mesh Prometheus gauges (managers, stacks, replication lag,
    # RPO breach) and raises a breach alert when replication is enabled + lagging.
    # services.mesh_metrics is CE-denied (mesh_* glob); at 30s this was the
    # highest-frequency offender of this pattern.
    if _module_available("services.mesh_metrics"):
        _scheduler.add_job(
            _run_mesh_metrics_refresh,
            IntervalTrigger(seconds=30),
            id="mesh_metrics_refresh",
            name="Mesh/replication metrics refresh + RPO-breach alert",
            replace_existing=True,
        )
    else:
        logger.info("mesh_metrics_refresh skipped (services.mesh_metrics not in this edition)")

    # --- Egress metering (every 15 min, flag-gated) ---
    # add-egress-billing — accumulate per-tenant bytes-out from MinIO per-bucket
    # traffic_sent_bytes into tenant_egress_usage_daily + tenants.egress_used_bytes.
    # OFF until EGRESS_METERING_ENABLED=true (so it's inert until pricing is set).
    if _module_available("services.egress_metering"):
        _scheduler.add_job(
            _run_egress_metering,
            IntervalTrigger(seconds=int(os.getenv("EGRESS_METERING_SECONDS", "900"))),
            id="egress_metering",
            name="Egress metering (MinIO per-bucket bytes-out -> tenant usage)",
            replace_existing=True,
        )
    else:
        logger.info("egress_metering skipped (services.egress_metering not in this edition)")

    # --- Relay metering (every 15 min, flag-gated) ---
    # tailnet-plan Group 8 — accumulate per-tenant MOD-relayed bytes from a MOD
    # DERP's per-account counters into tenant_relay_usage_daily +
    # tenants.relay_used_bytes. OFF until RELAY_METERING_ENABLED=true (inert until
    # relay pricing + a per-account DERP metrics source are configured).
    if _module_available("services.relay_metering"):
        _scheduler.add_job(
            _run_relay_metering,
            IntervalTrigger(seconds=int(os.getenv("RELAY_METERING_SECONDS", "900"))),
            id="relay_metering",
            name="Relay metering (MOD DERP per-account bytes -> tenant usage)",
            replace_existing=True,
        )
    else:
        logger.info("relay_metering skipped (services.relay_metering not in this edition)")

    # --- Heal stuck platform-node scan_status (every 5 min) ---
    # Same SQL as the startup migration in database.py:508-523, run on
    # an interval so post-startup transitions to pending_scan/scanning/
    # failed self-heal within minutes instead of waiting for the next
    # API restart. The orchestrator already short-circuits these at
    # workflow execute time (services/workflow_orchestrator.py); this
    # job closes the DB-invariant gap so smoke tests and audit views
    # match runtime behavior. Idempotent — no-op when nothing's stuck.
    _scheduler.add_job(
        _run_heal_node_scan_status,
        IntervalTrigger(minutes=5),
        id="heal_node_scan_status",
        name="Heal stuck platform-node scan_status (own + tenant clones)",
        replace_existing=True,
    )

    # --- Recovery sweep for stuck tenant-Harbor scan polls (every 60s) ---
    # The eager daemon-thread poller in routers/node_definitions can
    # die silently (process exit, unhandled exception inside the
    # worker). Without this sweep a row stuck at scanning/pending_scan
    # would never get a terminal outcome until the API restarts AND
    # someone re-clicks "Scan Now". The sweep is the safety net:
    # queries Harbor on each tick for the current state, writes the
    # outcome through the same _decide_harbor_scan_outcome helper as
    # the eager path. 15-minute per-row timeout caps the worst case.
    if _module_available("utils.tenant_harbor_client"):
        _scheduler.add_job(
            _run_sweep_stuck_harbor_scans,
            IntervalTrigger(seconds=60),
            id="sweep_stuck_harbor_scans",
            name="Recovery sweep for stuck tenant-Harbor scan polls",
            replace_existing=True,
        )
    else:
        logger.info("sweep_stuck_harbor_scans skipped (utils.tenant_harbor_client not in this edition)")

    # --- AI scheduler — shadow→promote sweep (daily 4:30am UTC) ---
    # Walks scheduler-vN.json metadata in MinIO platform-models/, promoting
    # any model in status='shadow' past its promote_after probation window
    # (default 24h, configured per-model). Status field is metadata-only;
    # hot-reload's `max(version)` filename parser is the load-bearing
    # mechanism. After scheduler-promote-gate landed, the trainer writes
    # status directly based on the gate decision — most uploads hit the
    # sweep already at status='production' or 'shadow' (with `-shadow`
    # filename), so the sweep is mostly housekeeping for legacy uploads.
    # Idempotent — re-running on the same set is a no-op.
    _scheduler.add_job(
        _run_shadow_promote_sweep_s3,
        CronTrigger(hour=4, minute=30),
        id="shadow_promote_sweep",
        name="AI scheduler — promote MinIO shadow models past their probation window",
        replace_existing=True,
    )

    # --- AI scheduler — auto-retrain trigger eval (daily 5:30am UTC) ---
    # Reads prediction_training_marker.recalibrations_since_last_train and
    # last_train_at; fires the retrain pipeline when EITHER:
    #   - counter ≥ AUTO_RETRAIN_RECALIBRATION_THRESHOLD (default 3) — enough
    #     new training data has accumulated since the last train to warrant
    #     a refresh.
    #   - now - last_train_at > AUTO_RETRAIN_SAFETY_NET_DAYS (default 90) —
    #     safety net so an idle deployment with low calibration churn still
    #     gets refreshed eventually.
    # Acquires the same Redis lock as POST /calibration/retrain so manual +
    # automatic retrains can't overlap. Successful retrain resets the counter
    # and stamps last_train_at.
    if _module_available("services.ml_retrain"):
        _scheduler.add_job(
            _run_auto_retrain_eval,
            CronTrigger(hour=5, minute=30),
            id="auto_retrain_eval",
            name="AI scheduler — fire retrain when threshold OR safety-net hit",
            replace_existing=True,
        )
    else:
        logger.info("auto_retrain_eval skipped (services.ml_retrain not in this edition)")

    # --- scheduler-drift-recal-scan — per-(node, hardware) drift re-cal ---
    # OFF by default (scheduler.drift_scan_enabled): the tick no-ops until the
    # setting is on, because run_drift_scan queues real calibration tasks.
    _scheduler.add_job(
        _run_drift_scan_tick,
        CronTrigger.from_crontab(
            str(_psr.get_setting("scheduler.drift_scan_cron") or "0 5 * * *")),
        id="scheduler_drift_scan",
        name="AI scheduler — queue pair-scoped re-calibration for drifting pairs",
        replace_existing=True,
    )

    # --- AI scheduler — calibration staleness eval (daily 5am UTC) ---
    # Sweeps HardwareProfile rows where calibration_status='calibrated' and
    # last_calibrated_at is older than the effective threshold (default 180d
    # in Phase 1; will become package-delta-driven in Phase 2). Flips
    # qualifying profiles to 'stale' so the orchestrator can auto-queue
    # recalibration on the next placement round. Driver-version-change
    # detection happens inline on every heartbeat — this is the time-based
    # safety net.
    _scheduler.add_job(
        _run_calibration_staleness_eval,
        CronTrigger(hour=5, minute=0),
        id="calibration_staleness_eval",
        name="AI scheduler — flip stale HardwareProfiles for recalibration",
        replace_existing=True,
    )

    # --- Leader lock renewal (every 30s, 300s TTL) ---
    #
    # Headroom matters here more than anywhere else: losing this lock does not
    # degrade one feature, it hands the entire scheduler to another process.
    # The original 60s renewal against a 120s TTL tolerated exactly ONE missed
    # run, and misses were routine — 132 of them in 30 minutes before the
    # executor defaults above were fixed.
    #
    # 30s against 300s tolerates nine consecutive misses, so a genuinely stuck
    # pool still triggers failover (which is the point of the lock) while
    # ordinary scheduling jitter never does.
    _LOCK_TTL_S = int(os.getenv("MOD_SCHEDULER_LOCK_TTL_S", "300"))

    def _renew_leader_lock():
        try:
            import redis
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
            r = redis.from_url(redis_url)
            r.expire("mod:scheduler:leader", _LOCK_TTL_S)
        except Exception:
            pass

    _scheduler.add_job(
        _renew_leader_lock,
        IntervalTrigger(seconds=30),
        id="leader_lock_renewal",
        name="Scheduler leader lock renewal",
        replace_existing=True,
        # Never let renewal be dropped for lateness. Everything else can run
        # late; this one losing a run costs the process its scheduler.
        misfire_grace_time=None,
    )

    # wizard-learning-from-feedback §1.5 — partition creator. Runs
    # on the 25th of every month at 02:00 UTC (off-peak, 6-day buffer
    # before month-end). Also fires once at startup so a freshly-
    # deployed instance catches up immediately.
    def _wizard_partition_creator():
        try:
            from services.wizard_partition_maintenance import ensure_future_partitions
            counts = ensure_future_partitions(months_ahead=2)
            logger.info(
                "wizard partition creator: %s partitions created across event tables",
                counts,
            )
        except Exception as e:
            logger.warning("wizard partition creator failed: %s", e)

    # GT.P2.3 — paid module; CE skips the job instead of warning monthly.
    if _module_available("services.wizard_partition_maintenance"):
        _scheduler.add_job(
            _wizard_partition_creator,
            CronTrigger(day=25, hour=2, minute=0),
            id="wizard_partition_creator_monthly",
            name="Wizard partition creator (monthly, 25th @ 02:00 UTC)",
            replace_existing=True,
        )
        # Startup catchup: run synchronously once so a freshly-deployed
        # instance has all required partitions before the first INSERT
        # arrives.
        try:
            _wizard_partition_creator()
        except Exception:
            logger.warning("wizard partition startup catchup failed", exc_info=True)

    # wizard-learning-from-feedback §5 — model registry refresh.
    # Reloads the in-memory registry from MinIO on a configurable
    # interval (default 3600s = 1h). Default platform-shared bucket
    # is empty in Phase 1 (no models trained yet); refreshes are
    # cheap no-ops until Phase 2+ trains the first artifact.
    def _wizard_registry_refresh():
        try:
            from services.wizard_ml.registry import get_registry
            get_registry().refresh_all()
        except Exception as e:
            logger.warning("wizard registry refresh failed: %s", e)

    # GT.P2.3 — wizard_ml is paid (served by the mod-wizard-pro sidecar in
    # CE+Pro); CE skips the in-process registry jobs.
    if _module_available("services.wizard_ml"):
        try:
            from services.wizard_ml.registry import refresh_interval_seconds as _wml_refresh_secs
            _wml_interval = _wml_refresh_secs()
        except Exception:
            _wml_interval = 3600

        _scheduler.add_job(
            _wizard_registry_refresh,
            IntervalTrigger(seconds=_wml_interval),
            id="wizard_ml_registry_refresh",
            name=f"Wizard ML model registry refresh (every {_wml_interval}s)",
            replace_existing=True,
        )
        # Startup load: refresh once synchronously so the registry is
        # populated by the time the first wizard request lands.
        try:
            _wizard_registry_refresh()
        except Exception:
            logger.warning("wizard registry startup refresh failed", exc_info=True)

    # wizard-learning-from-feedback §7.3 — weekly training trigger.
    # Sunday 03:00 UTC. Each kind fires its own daemon thread via
    # run_training_in_thread() so a slow fine-tune doesn't block the
    # scheduler thread or the next kind's trigger. Phase 1 these are
    # all data-threshold-gated stubs; Phase 2-5 each replaces its
    # train_<kind> with real fine-tune code.
    def _wizard_weekly_training_baseline():
        try:
            from services.wizard_ml.training_runner import run_training_in_thread
            for kind in ("reranker", "intent_classifier", "l2r", "audit_triage"):
                try:
                    run_training_in_thread(kind, scope="baseline")
                except Exception as e:
                    logger.warning(
                        "wizard training scheduler: failed to spawn %s thread: %s",
                        kind, e,
                    )
        except Exception as e:
            logger.warning("wizard weekly training failed: %s", e)

    if _module_available("services.wizard_ml"):
        _scheduler.add_job(
            _wizard_weekly_training_baseline,
            CronTrigger(day_of_week='sun', hour=3, minute=0),
            id="wizard_ml_weekly_training_baseline",
            name="Wizard ML weekly training — cross-tenant baseline (Sun 03:00 UTC)",
            replace_existing=True,
        )

    # --- tenant-resource-monitor: aggregator/leak-scan/digest ---
    # services.resource_monitor is CE-denied; all three ticks import it at
    # the top of the function body, ahead of (aggregator/leak-scan) or
    # outside (digest) their own try/except, so on CE this raised on every
    # fire. Gate registration instead.
    if _module_available("services.resource_monitor"):
        # Materialize TaskResourceAggregate rows for tasks completed since the
        # last tick. Idempotent — second run over the same window writes zero.
        _scheduler.add_job(
            _run_resource_monitor_aggregate,
            CronTrigger(minute=15),
            id="resource_monitor_aggregate",
            name="Resource-monitor aggregator (hourly :15)",
            replace_existing=True,
        )

        # Runs 15 min after the aggregator so the rolling 24h window
        # includes the freshly-aggregated rows. Emits one
        # RESOURCE_LEAK_DETECTED per (tenant, user) per 24h.
        _scheduler.add_job(
            _run_resource_monitor_leak_scan,
            CronTrigger(minute=30),
            id="resource_monitor_leak_scan",
            name="Resource-monitor leak scan (hourly :30)",
            replace_existing=True,
        )

        # Composes a digest email of the last 24h of offenders + top efficient
        # users; sends to every active platform admin.
        _scheduler.add_job(
            _run_resource_monitor_digest,
            CronTrigger(hour=3, minute=0),
            id="resource_monitor_digest",
            name="Resource-monitor daily digest (03:00 UTC)",
            replace_existing=True,
        )
    else:
        logger.info("resource_monitor_aggregate/leak_scan/digest skipped "
                    "(services.resource_monitor not in this edition)")

    # --- activation-lifecycle-emails: daily sweep (14:00 UTC) ---
    # Sends the next due activation email (welcome / progress nudge ladder /
    # bonus-expiry) to new Free-tier tenants until they complete 3 runs.
    # Idempotent; a missed day self-heals on the next sweep.
    def _activation_lifecycle_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("utils.activation_lifecycle")
        if _carve_m is None:
            return
        run_lifecycle_sweep = getattr(_carve_m, "run_lifecycle_sweep")
        db = _SL()
        try:
            run_lifecycle_sweep(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("activation_lifecycle sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("utils.activation_lifecycle"):
        _scheduler.add_job(
            _activation_lifecycle_tick,
            CronTrigger(hour=14, minute=0),
            id="activation_lifecycle_sweep",
            name="Activation lifecycle email sweep (14:00 UTC)",
            replace_existing=True,
        )
    else:
        logger.info("activation_lifecycle_sweep skipped (utils.activation_lifecycle not in this edition)")

    # --- stack-training-and-warm §2.1 — stack measurement sweep ---
    # Consumer for the async measurement queue (services/stack_calibration).
    # On stack create/import a PENDING measurement row is enqueued on the
    # SYSTEM queue (the Go worker ignores it by design); this API-side sweep runs
    # the measurement when the target accelerator is QUIESCENT (deferred while a
    # GPU task is in flight, keyed on in-flight TASKS so a zombie RUNNING run does
    # not falsely block it) and RATE-LIMITED to one at a time.
    #
    # Registered whenever the paid stack_calibration layer is present, mirroring
    # nim_idle_reclaim/llm_idle_reclaim: run_due_measurements self-gates (read
    # at call time), so the tick is a cheap no-op until the layer is switched on —
    # the job never runs any GPU work while it is off.
    #
    # FX.165 p1b — cadence is a DB platform setting (default 30 s, range 10..3600),
    # NOT an env var (house rule: no env tunables). WAKE.0 measured a 315 s p50
    # wait: rows sit on the SYSTEM queue until this 5-minute tick fires. 30 s plus
    # the enqueue kick (services/stack_calibration, a one-shot job at now+1 s)
    # closes the wait. Cadence is read at scheduler start — the same pattern as
    # fraud.mining_sweep_interval_minutes / time.check_interval_minutes.
    #
    # GT.D6: services.stack_calibration is Paid (open-core manifest deny) and
    # absent from the CE tree — guard at registration so the predictive
    # warm-time measurement sweep is never scheduled in CE (and its cadence
    # setting is never read there).
    if _module_available("services.stack_calibration"):
        _fx165p1b_tick_seconds = _stack_measurement_tick_seconds()
        _scheduler.add_job(
            _stack_measurement_tick,
            IntervalTrigger(seconds=_fx165p1b_tick_seconds),
            id="stack_measurement",
            name="Stack measurement sweep (quiescent-only, rate-limited)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("stack_measurement skipped (services.stack_calibration not in this edition)")

    # --- stack-training-and-warm §4.2 — zombie-RUNNING run reconcile ---
    # A run left in RUNNING/QUEUED whose NodeRuns are ALL terminal and which has
    # no in-flight task is a zombie: it poisons every "is the system busy" check
    # keyed on run status (notably nim_ephemeral.reclaim_idle's quiescence gate,
    # which then NEVER reclaims, so on-demand NIMs never sleep). This corrective
    # sweep finalizes such runs (status only + audit) so the busy-checks reflect
    # reality. Gated by scheduler.zombie_run_reconcile_enabled (default ON,
    # live); the function self-gates and DRY-RUN logs what it would finalize
    # if an admin flips the setting off.
    _scheduler.add_job(
        _run_zombie_run_reconcile,
        IntervalTrigger(minutes=int(_psr.get_setting("scheduler.zombie_run_reconcile_minutes") or 10)),
        id="zombie_run_reconcile",
        name="Zombie-RUNNING workflow-run reconcile (unpoison busy-checks)",
        replace_existing=True,
    )

    # --- GO.6e — stranded trigger-run re-dispatch ---
    # dispatch_trigger_run commits a QUEUED run then dispatches its root
    # tasks on a daemon thread; a uvicorn worker recycle kills the thread
    # and the run sits QUEUED with 0 NodeRuns / 0 tasks. The zombie
    # reconcile (above) finalizes it after 30 min, but it never RUNS. This
    # sweep re-drives it (same start_workflow_execution entry point) after
    # a short grace, capped per run; past the cap the zombie reconcile
    # finalizes it as FAILED. Self-gated by the
    # orchestration.trigger_run_redispatch_* platform settings (default ON)
    # — the function is a no-op when the flag is off.
    _scheduler.add_job(
        _run_trigger_run_redispatch_sweep,
        IntervalTrigger(minutes=5),
        id="trigger_run_redispatch_sweep",
        name="GO.6e stranded trigger-run re-dispatch (durable dispatch)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # BUG.B85 — a RUNNING run with zero NodeRuns and zero queued tasks can
    # never progress and blocks drains forever. Self-gated by the
    # orchestrator.ghost_run_reconcile_* platform settings (default ON, 15 min).
    _scheduler.add_job(
        _run_ghost_run_reconcile,
        IntervalTrigger(minutes=10),
        id="ghost_run_reconcile",
        name="BUG.B85 ghost-run reconcile (RUNNING with no node runs -> FAILED)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- launch-completion-megaplan 26.3 — MOD Core starter package ---
    # Every plan grants the free "MOD Core" node package; without it a new
    # tenant's palette is empty. New free nodes join, new plans get it bound,
    # and every subscribed tenant is reconciled (plan_grant entitlements only).
    def _starter_package_tick():
        from services.starter_package import sync_starter_package
        db = _get_db()
        try:
            sync_starter_package(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("starter_package sync raised: %s", e)
            db.rollback()
        finally:
            db.close()

    _scheduler.add_job(
        _starter_package_tick,
        IntervalTrigger(minutes=int(os.getenv("STARTER_PACKAGE_SYNC_MINUTES", "15"))),
        id="starter_package_sync",
        name="MOD Core starter package sync (default palette for every plan)",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        replace_existing=True,
    )

    # --- launch-completion-megaplan 19.5 — Field Capture add-on bundle ---
    # §K sells the field client as a Group G bundle: the NodePackage row, its
    # Group K nodes and both membership tables self-seed at boot (same on-demand
    # pattern as the starter package above), so the store always lists it and
    # the field-capture entitlement gate is live.
    def _field_addon_bundle_tick():
        # CE build: services/field_addon_bundle.py is denied+stripped
        # (open-core/manifest.yaml `CORE/API/services/field_*.py`), so the
        # bundle cannot be seeded — return early instead of raising every tick.
        if not _module_available("services.field_addon_bundle"):
            return None  # not in this edition — no Field Capture bundle
        _carve_m = optional_paid("services.field_addon_bundle")
        if _carve_m is None:
            return
        ensure_bundle = getattr(_carve_m, "ensure_bundle")
        db = _get_db()
        try:
            ensure_bundle(db)
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("field_addon_bundle seed raised: %s", e)
            db.rollback()
        finally:
            db.close()

    _scheduler.add_job(
        _field_addon_bundle_tick,
        IntervalTrigger(minutes=30),
        id="field_addon_bundle_seed",
        name="Field Capture add-on catalog bundle (Group G listing + entitlement gate)",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
        replace_existing=True,
    )

    # --- GG.MKT.CR (launch-completion-megaplan) — Marketplace credit-sale
    # report outbox sender. A credit-funded purchase is fulfilled entirely
    # by Core (see services/closure_fulfilment.py:buy_with_credits); this
    # tick reports it to the Marketplace afterwards, purely so the
    # publisher's revenue share/payouts count it. Gated on the
    # marketplace.credit_sale_report_enabled platform setting (default
    # off, registered in services/marketplace_credit_sale_report.py) —
    # send_pending() itself no-ops when the setting is off, so this job is
    # a cheap no-op query until the setting is flipped.
    def _marketplace_credit_sale_report_tick():
        _mcsr = optional_paid("services.marketplace_credit_sale_report")
        if _mcsr is None:
            return
        db = _get_db()
        try:
            _mcsr.send_pending(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("marketplace_credit_sale_report tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.marketplace_credit_sale_report"):
        _scheduler.add_job(
            _marketplace_credit_sale_report_tick,
            IntervalTrigger(minutes=15),
            id="marketplace_credit_sale_report",
            name="Marketplace credit-sale report outbox sender (revenue-share only)",
            replace_existing=True,
        )
    else:
        logger.info("marketplace_credit_sale_report skipped (services.marketplace_credit_sale_report not in this edition)")

    # --- GO.8 (launch-completion-megaplan) — corpus gate weekly run ---
    # Weekly off-peak enqueue of the manifest's fast subset through the
    # corpus harness's existing enqueue path, plus an every-6h evaluation
    # of the latest attempt against the quality bars. Both are gated by the
    # node_build.corpus_gate_weekly_enabled platform setting (default off —
    # the job functions return immediately when it is false). A bar breach
    # writes one WIZARD_RELEASE_BLOCKED audit event and flips
    # node_build.wizard_release_blocked to true (never auto-cleared).
    from node_builder.variant_test import corpus_gate as _corpus_gate

    def _corpus_gate_weekly_enqueue():
        _corpus_gate.register_weekly_settings()
        db = _get_db()
        try:
            summary = _corpus_gate.enqueue_weekly(db)
            if summary.get("enqueued"):
                logger.info("corpus_gate weekly enqueue: %s", summary)
        except Exception as e:  # noqa: BLE001
            logger.exception("corpus_gate weekly enqueue raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    def _corpus_gate_weekly_evaluate():
        _corpus_gate.register_weekly_settings()
        db = _get_db()
        try:
            summary = _corpus_gate.evaluate_weekly(db)
            if summary.get("breached"):
                logger.warning("corpus_gate weekly evaluate BREACHED bars "
                               "— wizard_release_blocked set: %s",
                               summary.get("failing"))
        except Exception as e:  # noqa: BLE001
            logger.exception("corpus_gate weekly evaluate raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    _scheduler.add_job(
        _corpus_gate_weekly_enqueue,
        CronTrigger(day_of_week="sun", hour=3, minute=17),  # 03:17 UTC
        id="corpus_gate_weekly_enqueue",
        name="GO.8 corpus gate weekly fast-subset enqueue (Sun 03:17 UTC)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _corpus_gate_weekly_evaluate,
        IntervalTrigger(hours=6),
        id="corpus_gate_weekly_evaluate",
        name="GO.8 corpus gate weekly attempt evaluation (every 6h)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GO.8 (23.7.s2) — weekly change-gate sweep + report ------
    # REPORT FIRST, then enqueue: the report describes the results that
    # gathered SINCE THE PREVIOUS sweep (the runs that sweep enqueued last
    # week), compared with s1's is_regression — a score drop past
    # node_build.variant_change_gate_score_drop vs the incumbent, or a good→
    # bad verdict. Absolute-low variants are listed separately as
    # below_threshold, never as regressed. The report is persisted as an
    # ADMIN_SYSTEM_MAINTENANCE audit row (the audit_metadata `event` key
    # names it) BEFORE anything enqueues, so admins/API can read it back via
    # change_gate.latest_weekly_report(db). This week's runs are enqueued
    # afterwards through the existing queue path. Gated by
    # node_build.variant_change_gate_enabled.
    from node_builder.variant_test import change_gate as _cg

    def _change_gate_weekly_sweep():
        _cg.register_settings()
        db = _get_db()
        try:
            summary = _cg.run_weekly_sweep(db)
            logger.info(
                "change_gate weekly sweep: %d enqueued, %d total, "
                "%d regressed, %d below_threshold",
                summary.get("enqueued", 0),
                summary.get("total", 0),
                summary.get("regressed", 0),
                summary.get("below_threshold", 0),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("change_gate weekly sweep raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    _scheduler.add_job(
        _change_gate_weekly_sweep,
        CronTrigger(day_of_week="sun", hour=3, minute=25),  # 03:25 UTC
        id="change_gate_weekly_sweep",
        name="GO.8 change gate weekly sweep + report (Sun 03:25 UTC)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GY.C5: SIEM audit-event export shipper ---
    # Ships permission_audit_logs rows past the siem.export_checkpoint to
    # the configured SIEM endpoint (RFC 5424 syslog / CEF / Splunk HEC /
    # OTLP) in bounded batches; the checkpoint advances only on a fully
    # successful send. Runs every 10s as a heartbeat — the tick re-reads
    # siem.enabled and siem.interval_seconds on every pass, so disabling
    # or changing cadence needs no restart.
    if _module_available("services.siem_export"):
        _carve_m = optional_paid("services.siem_export")
        if _carve_m is None:
            return
        _siem_run = getattr(_carve_m, "run_export_tick")

        def _siem_export_tick():
            from database import SessionLocal as _SL

            db = _SL()
            try:
                result = _siem_run(db)
                if result and result.get("status") not in ("disabled", "no_pending", "skipped_interval"):
                    logger.info("siem export tick: %s", result)
            except Exception as e:  # noqa: BLE001 — never kill the scheduler
                logger.exception("siem export tick raised: %s", e)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                db.close()

        # siem_export.siem_interval_heartbeat_seconds() documents the fixed
        # 10s heartbeat; import-time resolution here would drag the shipper
        # module into every scheduler build, so the value is inlined.
        _scheduler.add_job(
            _siem_export_tick,
            IntervalTrigger(seconds=10),
            id="siem_export",
            name="SIEM audit-event export shipper (GY.C5)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # --- Q9 part 1: Vault seal-state monitor (30s heartbeat, throttled) ---
    # After the move to Shamir + MANUAL unseal, a Vault restart comes back
    # SEALED and secret-dependent paths fail until humans unseal it. This
    # probes /v1/sys/seal-status and, on a transition, writes ONE
    # VAULT_SEALED audit + admin alert (VAULT_UNSEALED on recovery). Cadence
    # is throttled to the platform setting vault.seal_check_interval_seconds
    # (default 60s); <=0 disables it without a restart. Only the scheduler
    # leader reaches this job.
    _scheduler.add_job(
        vault_seal_check_tick,
        IntervalTrigger(seconds=30),
        id="vault_seal_check",
        name="Vault seal-state monitor (manual-unseal alerting)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GSEC Q111 part 1 + 2: clock-drift and certificate-expiry monitors ---
    # Both are 1-minute heartbeat jobs throttled inside the tick to the
    # platform setting time.check_interval_minutes (default 10 min), so a
    # cadence change takes effect without a restart and <=0 disables both.
    # Clock drift alerts on time.max_clock_skew_seconds (default 5s); cert
    # expiry warns per source inside tls.cert_expiry_warn_days (default 21
    # days). Both alert through the existing admin-alert path (structured
    # ERROR log + audit event + best-effort admin email) and transition-state
    # machine, so a persistently bad value cannot spam the admin. Only the
    # scheduler leader reaches these jobs.
    _scheduler.add_job(
        clock_skew_check_tick,
        IntervalTrigger(minutes=1),
        id="clock_skew_check",
        name="GSEC Q111 clock-drift monitor (NIST AU-8 alerting)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        cert_expiry_check_tick,
        IntervalTrigger(minutes=1),
        id="cert_expiry_check",
        name="GSEC Q111 certificate-expiry monitor (read-only)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GY.C12: continuous conformance engine run ---
    # Every registered check runs isolated (exception → status error),
    # results persist append-only, pass→fail drift emits a
    # CONFORMANCE_DRIFT audit/security event (services.conformance).
    # Interval comes from the platform setting
    # `conformance.run_interval_minutes` (default 360 min); the tick
    # re-reads it on every run, so 0 disables the job without a
    # restart. Only the scheduler leader reaches this job.
    # FX.162.p1 (owner Q157): compliance is paid-only — under CE the whole
    # conformance engine is absent, so this job is never scheduled.
    if is_paid_tree():
        _scheduler.add_job(
            conformance_run_tick,
            IntervalTrigger(minutes=conformance_run_interval_minutes()),
            id="conformance_run",
            name="GY.C12 continuous conformance engine (scheduled run)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("conformance_run skipped (compliance is not in this edition)")

    # --- GSEC R-113: daily temp-prefix object-storage sweep (05:20 UTC) ---
    # Aborts abandoned multipart uploads and removes unreferenced objects
    # under uploads/temp/ + downloads/temp/ in tenant buckets. Gated by the
    # platform setting storage.temp_sweep_enabled (default off) — the tick
    # re-reads it on every run, so it no-ops until an admin enables it.
    # Off-peak and offset from the other 05:xx jobs (05:00/05:15/05:30).
    # Temp objects are transient staging, not records, so removal is
    # permitted (ISO 27001) — legal-held tenants are skipped and every
    # sweep writes a per-tenant append-only audit row (counts only).
    def _temp_object_sweep_tick():
        from services.temp_object_sweep import run_temp_object_sweep
        try:
            result = run_temp_object_sweep()
            if result.get("disabled"):
                return
            logger.info("temp_object_sweep tick: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("temp_object_sweep tick raised: %s", e)

    _scheduler.add_job(
        _temp_object_sweep_tick,
        CronTrigger(hour=5, minute=20),
        id="temp_object_sweep",
        name="Tenant temp-prefix object sweep (GSEC R-113)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- Fortress 3.5: daily scan-staging orphan sweep (05:25 UTC) ---
    # Docker-path workers stage output scan candidates under scan-staging/{task_id}/
    # and defer-remove them at Run() exit; a crash/kill in between leaves the
    # scratch copies behind. This sweep removes prefixes whose task is terminal
    # (COMPLETED/FAILED/CANCELLED) or whose objects aged past
    # fortress.scan_staging_grace_seconds (default 3600). Gated by
    # fortress.scan_staging_sweep_enabled (default on) — the tick re-reads it,
    # so disabling is a DB change with no restart. Scratch copies, not records,
    # so removal is permitted (ISO 27001); nothing outside scan-staging/ is
    # ever touched and the per-tenant audit row carries counts only.
    def _scan_staging_sweep_tick():
        _carve_m = optional_paid("services.scan_staging_sweep")
        if _carve_m is None:
            return
        run_scan_staging_sweep = getattr(_carve_m, "run_scan_staging_sweep")
        try:
            result = run_scan_staging_sweep()
            if result.get("disabled"):
                return
            logger.info("scan_staging_sweep tick: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("scan_staging_sweep tick raised: %s", e)

    if _module_available("services.scan_staging_sweep"):
        _scheduler.add_job(
            _scan_staging_sweep_tick,
            CronTrigger(hour=5, minute=25),
            id="scan_staging_sweep",
            name="Tenant scan-staging orphan sweep (Fortress 3.5)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("scan_staging_sweep skipped (services.scan_staging_sweep not in this edition)")

    # --- GY.C10: access-review campaign schedule ---
    # Snapshot + open every 'scheduled' campaign whose due_at has passed
    # (the access-review campaign service open_due_campaigns). Gated by the
    # platform setting access_review.schedule_enabled (OFF by default) —
    # the tick re-reads it on every run, so turning it on/off is a DB
    # change with no restart. Only the scheduler leader reaches this job.
    def _access_review_campaigns_tick():
        from services.platform_settings import get_setting as _ps_get
        _carve_m = optional_paid("services.access_review_campaigns")
        if _carve_m is None:
            return
        open_due_campaigns = getattr(_carve_m, "open_due_campaigns")
        from database import SessionLocal as _SL
        try:
            probe = _get_db()
            try:
                enabled = _ps_get("access_review.schedule_enabled", db=probe)
            finally:
                probe.close()
            if not enabled:
                return
        except Exception as e:  # noqa: BLE001 — fail closed (no open)
            logger.warning("access_review_campaigns tick: setting read "
                           "failed; skipping (%s)", e)
            return
        db = _SL()
        try:
            opened = open_due_campaigns(db)
            if opened:
                logger.info(
                    "access_review_campaigns tick: opened %d campaign(s): %s",
                    len(opened), opened)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("access_review_campaigns tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.access_review_campaigns"):
        _scheduler.add_job(
            _access_review_campaigns_tick,
            IntervalTrigger(minutes=15),
            id="access_review_campaigns",
            name="GY.C10 access-review campaign schedule (open due campaigns)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("access_review_campaigns skipped (services.access_review_campaigns not in this edition)")

    # --- FX.71 (job part, Q54=A): quarterly platform access review ---
    # Ensure THIS quarter's platform-scope campaign (MOD's own
    # privileged accounts: platform admins + KC realm admins + manual
    # Vault/Harbor/git/cloud rows) exists, once the quarter's day-of-
    # quarter reaches access_review.platform_quarter_day. Gated by
    # access_review.platform_schedule_enabled (OFF by default); the
    # service (open_due_platform_campaigns) is idempotent — one
    # campaign per quarter, keyed by the YYYY-Qn title.
    def _platform_access_review_tick():
        from services.platform_settings import get_setting as _ps_get
        _carve_m = optional_paid("services.access_review_campaigns")
        if _carve_m is None:
            return
        open_due_platform_campaigns = getattr(_carve_m, "open_due_platform_campaigns")
        _platform_quarter_day = getattr(_carve_m, "_platform_quarter_day")
        from database import SessionLocal as _SL
        try:
            probe = _get_db()
            try:
                enabled = _ps_get(
                    "access_review.platform_schedule_enabled", db=probe)
                quarter_day = _ps_get(
                    "access_review.platform_quarter_day", db=probe)
            finally:
                probe.close()
            if not enabled:
                return
            if _platform_quarter_day() < int(quarter_day or 1):
                return
        except Exception as e:  # noqa: BLE001 — fail closed (no create)
            logger.warning("platform_access_review tick: settings read "
                           "failed; skipping (%s)", e)
            return
        db = _SL()
        try:
            created = open_due_platform_campaigns(db)
            if created:
                logger.info(
                    "platform_access_review tick: generated campaign(s): %s",
                    created)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.exception("platform_access_review tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.access_review_campaigns"):
        _scheduler.add_job(
            _platform_access_review_tick,
            IntervalTrigger(minutes=15),
            id="platform_access_review",
            name="FX.71 quarterly platform access-review campaign",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("platform_access_review skipped (services.access_review_campaigns not in this edition)")

    # --- FX.114 (owner Q101=A): API key lifecycle (daily) ---
    # services.service_accounts.lifecycle_tick does the three
    # lifetime jobs in one daily sweep (best-effort, never raises):
    #   1. 14/3-day expiry reminders, deduped per key by the
    #      reminder_14d_sent / reminder_3d_sent flags (a failed send
    #      leaves the flag unset and retries next tick);
    #   2. auto-DISABLE (never delete) of live keys unused for
    #      auth.api_key_auto_disable_unused_days (default 90), audited
    #      API_KEY_AUTO_DISABLED;
    #   3. auto-disable of rotation "old" keys whose overlap window
    #      (auth.api_key_rotation_overlap_hours, default 24h) has
    #      ended. 08:45 UTC — clear of the 07:30 licence check and the
    #      05:15 inactive-disable jobs.
    def _fx114_api_key_lifecycle_tick():
        from database import SessionLocal as _SL
        from services import service_accounts as _sa
        db = _SL()
        try:
            counters = _sa.lifecycle_tick(db)
            if (counters.get("reminded_14d") or counters.get("reminded_3d")
                    or counters.get("auto_disabled_unused")
                    or counters.get("auto_disabled_rotation")):
                logger.info("fx114_api_key_lifecycle tick: %s", counters)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx114_api_key_lifecycle tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx114_api_key_lifecycle_tick,
        CronTrigger(hour=8, minute=45),  # 08:45 UTC daily
        id="fx114_api_key_lifecycle",
        name="FX.114 API key expiry reminders + unused/rotation auto-disable",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GSEC FX.152 part 3 (owner decision Q139=B): reminder emails ---
    # services/api_key_reminders.py sends the 14/3-day expiry reminders
    # (deduped per key per horizon by the FX.114 reminder flags) and the
    # weekly stale-key digest to each tenant's admins. Both gates
    # (api_keys.expiry_reminders_enabled / api_keys.stale_report_enabled)
    # are re-read every tick. REPORTING ONLY — this job never disables a key.
    # 09:10 UTC daily, clear of the 08:45 FX.114 sweep.
    def _fx152p3_expiry_reminder_tick():
        from database import SessionLocal as _SL
        _fx152 = optional_paid("services.api_key_reminders")
        if _fx152 is None:
            return
        db = _SL()
        try:
            counters = _fx152.run_expiry_reminders(db)
            if counters.get("sent"):
                logger.info("fx152p3 expiry reminder tick: %s", counters)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx152p3 expiry reminder tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.api_key_reminders"):
        _scheduler.add_job(
            _fx152p3_expiry_reminder_tick,
            CronTrigger(hour=9, minute=10),  # 09:10 UTC daily
            id="fx152p3_api_key_expiry_reminders",
            name="GSEC FX.152 p3 API key expiry reminder emails (14d/3d)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx152p3_api_key_expiry_reminders skipped (services.api_key_reminders not in this edition)")

    def _fx152p3_stale_digest_tick():
        from database import SessionLocal as _SL
        _fx152 = optional_paid("services.api_key_reminders")
        if _fx152 is None:
            return
        db = _SL()
        try:
            counters = _fx152.run_stale_digest(db)
            if counters.get("sent"):
                logger.info("fx152p3 stale digest tick: %s", counters)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx152p3 stale digest tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.api_key_reminders"):
        _scheduler.add_job(
            _fx152p3_stale_digest_tick,
            CronTrigger(day_of_week="mon", hour=9, minute=30),  # 09:30 UTC Mondays
            id="fx152p3_api_key_stale_digest",
            name="GSEC FX.152 p3 weekly stale API-key digest (tenant admins)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx152p3_api_key_stale_digest skipped (services.api_key_reminders not in this edition)")

    # --- GY.C0 follow-up: passive licence-expiry detection (daily) ---
    # A licence that silently expires is never noticed by the explicit
    # /license/update transition hooks. This tick re-evaluates the stored
    # licence via utils.license_client.validate_license, compares with
    # license.last_known_state, and on valid -> grace/invalid calls the
    # SAME compliance_profiles.on_license_dropped_to_ce hook (audit +
    # admin notice). Also warns admins license.expiry_warn_days (default
    # 14) ahead of expiry, once per expiry date. Idempotent: unchanged
    # state -> no writes. Best-effort — the tick never raises.
    def _license_expiry_check_tick():
        _carve_m = optional_paid("services.license_expiry_check")
        if _carve_m is None:
            return
        run_license_expiry_check = getattr(_carve_m, "run_license_expiry_check")
        try:
            counters = run_license_expiry_check()
            if counters.get("transitioned") or counters.get("warned"):
                logger.info("license_expiry_check tick: %s", counters)
        except Exception as e:  # noqa: BLE001 — never fatal
            logger.warning("license_expiry_check tick raised: %s", e)

    if _module_available("services.license_expiry_check"):
        _scheduler.add_job(
            _license_expiry_check_tick,
            CronTrigger(hour=7, minute=30),  # 07:30 UTC
            id="license_expiry_check",
            name="GY.C0 follow-up passive licence-expiry check (daily)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("license_expiry_check skipped (services.license_expiry_check not in this edition)")

    # --- FX.42: DSAR request reminders (hourly) ---
    # Personal-data (DSAR) requests due within 7 / 3 days get an email to
    # platform admins via the existing email service; per-request flags
    # keep it idempotent (a request that was extended uses its extended
    # deadline). Own session, off the request path; best-effort.
    def _dsar_reminders_tick():
        from database import SessionLocal as _SL
        dsar_tracker_service = optional_paid("services.dsar_tracker_service")
        if dsar_tracker_service is None:
            return
        db = _SL()
        try:
            counters = dsar_tracker_service.reminder_tick(db)
            if counters.get("reminded"):
                logger.info("dsar_reminders tick: %s", counters)
        except Exception as e:  # noqa: BLE001 — never fatal
            logger.warning("dsar_reminders tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.dsar_tracker_service"):
        _scheduler.add_job(
            _dsar_reminders_tick,
            IntervalTrigger(hours=1),
            id="dsar_reminders",
            name="FX.42 DSAR request 7/3-day-before-due reminders",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("dsar_reminders skipped (services.dsar_tracker_service not in this edition)")

    # --- GSEC FX.52 (owner Q35=A): weekly remediation-SLA overdue report ---
    # services.vuln_overdue_report.run_overdue_report: parses the GSEC
    # register (vuln.register_path setting) through tools.register_due_dates
    # — the same due-date authority the conformance check
    # vuln.remediation_overdue uses — and emails the open items past their
    # SLA due date (Critical 15d / KEV 14d / High 30d / Medium 90d / Low 180d
    # from the found/confirmed date) without an unexpired `ACCEPTED YYYY-MM-DD`
    # risk-acceptance sign-off to every active platform admin. Gated by
    # vuln.overdue_email_enabled (default true, re-read every tick). Mondays
    # 06:00 UTC — clear of the 05:15 inactive-disable and 07:00 usage-alert
    # jobs. Best-effort: the tick never raises.
    def _vuln_overdue_report_tick():
        from services.vuln_overdue_report import run_overdue_report
        db = _get_db()
        try:
            summary = run_overdue_report(db)
            logger.info("vuln_overdue_report tick: %s", summary)
        except Exception as e:  # noqa: BLE001 — never fatal
            logger.warning("vuln_overdue_report tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _vuln_overdue_report_tick,
        CronTrigger(day_of_week='mon', hour=6, minute=0),  # 06:00 UTC
        id="vuln_overdue_report",
        name="GSEC FX.52 weekly remediation-SLA overdue report (admins)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # FX.117 PART 2 (owner decision Q104=A) — customer secret store:
    # daily expiry sweep. Secrets with an optional expires_at remind
    # their owner N days before (secrets.expiry_reminder_days platform
    # setting, default [3, 14], deduped via secrets.expiry_reminders_sent)
    # and get a SOFT expired stamp once past expiry (expired_notified_at;
    # NEVER a hard delete — ISO 27001). The read path itself refuses
    # injection of expired secrets (403 + audited); this sweep only
    # reminds + marks. 04:32 UTC, clear of the 04:17 MFA and 04:45
    # FX.110 sweeps.
    # ------------------------------------------------------------------
    def _fx117p2_secrets_expiry_tick():
        db = _get_db()
        try:
            from services import secrets_service as _fx117
            result = _fx117.run_secrets_expiry_sweep(db)
            logger.info("fx117 secrets expiry tick: %s", result)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx117 secrets expiry tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx117p2_secrets_expiry_tick,
        CronTrigger(hour=4, minute=32),  # 04:32 UTC daily
        id="fx117_secrets_expiry",
        name="FX.117 secret expiry (remind + soft-expire)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # FX.110 (owner decision Q97=A) — outside collaborators: daily
    # expiry job. Every external account + grant carries an end date
    # (external_collab.default_expiry_days, tenant setting, default 90):
    # this tick reminds the inviter N days before (deduped) and then
    # SOFT-disables expired access — grants get is_active=False +
    # revoked_at + revoke_reason, expired external users get
    # status=DEACTIVATED — always audited, NEVER a hard delete.
    # 04:45 UTC, clear of the 04:10 fixture-drift and 05:15
    # inactive-disable jobs.
    # ------------------------------------------------------------------
    def _fx110_external_expiry_tick():
        db = _get_db()
        try:
            from services import outside_collaborators as _fx110
            result = _fx110.run_external_expiry_tick(db)
            logger.info("fx110_external_expiry tick: %s", result)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx110_external_expiry tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx110_external_expiry_tick,
        CronTrigger(hour=4, minute=45),  # 04:45 UTC daily
        id="fx110_external_expiry",
        name="FX.110 outside-collaborator expiry (remind + soft-disable)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # FX.103 part 2 (owner decision Q89=A) — node retirement scheduler:
    # daily 04:05 UTC sweep (services/node_retirement.run_retirement_sweep):
    # (a) finds nodes marked 'deprecated' that carry a retirement date;
    # (b) refuses to retire anything whose notice period (default 6) or
    # 12-month runnable floor has not elapsed (lifecycle.* platform
    # settings); (c) sends the one-time 6-month deprecation notice email
    # to the tenant admins of every tenant whose workflows use the node
    # (stamped node_definitions.deprecation_notice_sent_at so it never
    # re-sends); (d) on the retirement date flips status to 'retired'
    # (soft — nothing is deleted) and writes the NODE_RETIRED audit row
    # via services.node_deprecation.apply_node_status. Pinned runs keep
    # working until the date; after it, the dispatch run guard
    # (worker_tasks.enqueue_task_internal) fails them with the upgrade
    # pointer (POST /workflows/{id}/upgrade-nodes).
    # ------------------------------------------------------------------
    def _fx103_node_retirement_tick():
        db = _get_db()
        try:
            from services import node_retirement as _fx103
            result = _fx103.run_retirement_sweep(db)
            if result.get("notices_done") or result.get("retired") \
                    or result.get("refused"):
                logger.info("fx103_node_retirement tick: %s", result)
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx103_node_retirement tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.node_deprecation"):
        _scheduler.add_job(
            _fx103_node_retirement_tick,
            CronTrigger(hour=4, minute=5),  # 04:05 UTC daily
            id="fx103_node_retirement",
            name="FX.103 node retirement (notice + flip + audit)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx103_node_retirement skipped (services.node_deprecation not in this edition)")

    # ------------------------------------------------------------------
    # FX.103 p3a (owner decision Q89=A) — the RETIREMENT SCHEDULER proper:
    # daily 04:15 UTC (services/node_retirement_scheduler.run_retirement_schedule)
    # flips a 'deprecated' node to 'retired' ONLY once deprecated_at + the
    # runnable window (retirement.min_runnable_months, default 12 months —
    # the validator refuses anything below 12 unless the platform override
    # flag retirement.allow_below_minimum_window is turned on deliberately)
    # has passed AND no blocking condition holds (a node still used by an
    # ACTIVE schedule trigger is HELD and reported, because retiring it
    # would silently break a standing cron pipeline). Soft state only, one
    # NODE_RETIRED audit row per transition via the part-1 write path,
    # idempotent, and retirement.dry_run reports the flips without touching
    # a row.
    # ------------------------------------------------------------------
    def _fx103p3a_retirement_tick():
        db = _get_db()
        try:
            from services import node_retirement_scheduler as _fx103p3a
            result = _fx103p3a.run_retirement_schedule(db)
            if result["retired"] or result["held"] or result["refused"] \
                    or result["would_retire"]:
                logger.info("fx103p3a_node_retirement tick: %s", {
                    k: v for k, v in result.items()
                    if k not in ("held_nodes", "retired_nodes")})
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx103p3a_node_retirement tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx103p3a_retirement_tick,
        CronTrigger(hour=4, minute=15),  # 04:15 UTC daily
        id="fx103p3a_node_retirement",
        name="FX.103 p3a node retirement (window + blocking conditions)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # FX.103 p3b (owner decision Q89=A) — the 6-MONTH NOTICE EMAILS before a
    # deprecated node is retired: daily 04:20 UTC (AFTER part 2's 04:05 and
    # part 3a's 04:15 tick, so the notice reads the state both already
    # settled on). For every tenant whose workflows use a deprecated node
    # whose retirement date (max(deprecated_at + retirement.min_runnable_months,
    # retire_after) — the SAME date p3a flips on) is at or before now +
    # retirement.notice_lead_months (default 6) and still in the future, ONE
    # email to that tenant's admins, honouring the per-user notification
    # preferences on the 'email' channel (opt-outs respected). Exactly-once
    # per (node, tenant, notice stage) through LifecycleEmailLog's unique
    # (tenant_id, email_key) pair — no new table, no migration — and every
    # dispatch writes a NODE_DEPRECATION_NOTICE_SENT audit row.
    # ------------------------------------------------------------------
    def _fx103p3b_notice_tick():
        db = _get_db()
        try:
            from services import node_deprecation_notices as _fx103p3b
            result = _fx103p3b.run_deprecation_notice_emails(db)
            if result["sent"] or result["would_send"] \
                    or result["already_noticed"]:
                logger.info("fx103p3b_deprecation_notices tick: %s", {
                    k: v for k, v in result.items()
                    if k != "noticed_nodes"})
        except Exception as e:  # noqa: BLE001 — never fatal
            db.rollback()
            logger.warning("fx103p3b_deprecation_notices tick raised: %s", e)
        finally:
            db.close()

    if _module_available("services.node_deprecation_notices"):
        _scheduler.add_job(
            _fx103p3b_notice_tick,
            CronTrigger(hour=4, minute=20),  # 04:20 UTC daily
            id="fx103p3b_deprecation_notices",
            name="FX.103 p3b deprecation notice emails (6-month lead)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx103p3b_deprecation_notices skipped (services.node_deprecation_notices not in this edition)")

    # ------------------------------------------------------------------
    # GSEC FX.156 part 1 (owner decision Q143=B) — platform security
    # alert-rules engine: every 5 minutes evaluate the enabled rules
    # over audit rows since the last run (services/
    # security_alert_engine.run_rules). Findings dedupe into
    # security_alerts by (rule_key, tenant_id, subject); NEW rows are
    # the part-2 email/digest + ack path's input (returned, not
    # re-queued). PART 3 adds the remaining rules to the same sweep —
    # no new job. The tick never raises into the scheduler.
    # ------------------------------------------------------------------
    def _fx156_security_alert_rules_tick():
        if optional_paid("services.security_alert_engine") is None:
            # FX.162.p5 — CE carve: the alert-rules engine is a paid
            # compliance service; the tick does nothing on CE.
            return
        _fx156d = optional_paid("services.security_alert_delivery")
        if _fx156d is None:
            return
        _fx156 = optional_paid("services.security_alert_engine")
        if _fx156 is None:
            return
        db = _get_db()
        try:
            new_alerts = _fx156.run_rules(db)
            if new_alerts:
                logger.info(
                    "fx156 security alert rules tick: %d new alert(s): %s",
                    len(new_alerts),
                    [f"{a.rule_key}:{a.subject}" for a in new_alerts])
                # PART 2 — immediate email for the NEW rows at/above
                # security_alerts.email_min_severity (platform-level
                # alerts -> platform admins, tenant alerts -> that
                # tenant's security recipients). The sender never
                # raises (each recipient failure is logged + skipped).
                sent = _fx156d.send_new_alert_emails(db, new_alerts)
                if sent:
                    logger.info(
                        "fx156 security alert rules tick: %d alert "
                        "email(s) dispatched", sent)
        except Exception as e:  # noqa: BLE001 — never fatal
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("fx156 security alert rules tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx156_security_alert_rules_tick,
        IntervalTrigger(minutes=5),
        id="fx156_security_alert_rules",
        name="GSEC FX.156 security alert-rules engine (5-min sweep)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # GSEC FX.156 part 2 — daily digest: one summary email per
    # recipient group (platform admins / one tenant's security
    # recipients) listing every UNACKED alert seen in the last 24h
    # (services/security_alert_delivery.send_daily_digest). Gated by
    # security_alerts.digest_enabled. The tick never raises into the
    # scheduler.
    # ------------------------------------------------------------------
    def _fx156_security_alert_digest_tick():
        from utils.paid_import import optional_paid
        if optional_paid("services.security_alert_delivery") is None:
            # FX.162.p5 — CE carve: no security-alert delivery service.
            return
        _fx156d = optional_paid("services.security_alert_delivery")
        if _fx156d is None:
            return
        db = _get_db()
        try:
            sent = _fx156d.send_daily_digest(db)
            if sent:
                logger.info("fx156 security alert digest tick: %d "
                            "email(s)", sent)
        except Exception as e:  # noqa: BLE001 — never fatal
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("fx156 security alert digest tick raised: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx156_security_alert_digest_tick,
        CronTrigger(hour=7, minute=10),  # 07:10 UTC daily
        id="fx156_security_alert_digest",
        name="GSEC FX.156 security alerts daily digest (07:10 UTC)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # GSEC FX.49 (owner decision Q32=A) — break-glass custody expiry
    # sweep: every 5 minutes flip approved-but-past-expiry break-glass
    # use requests to 'expired' (services/break_glass_custody.
    # expire_sweep) so an unused approval never outlives its window.
    # The tick never raises into the scheduler.
    # ------------------------------------------------------------------
    def _fx49_break_glass_expire_sweep_tick():
        from utils.paid_import import optional_paid
        if optional_paid("services.break_glass_custody") is None:
            # FX.162.p5 — CE carve: no break-glass custody service.
            return
        _fx49 = optional_paid("services.break_glass_custody")
        if _fx49 is None:
            return
        db = _get_db()
        try:
            n = _fx49.expire_sweep(db)
            if n:
                logger.info("fx49 break-glass expire sweep: %d "
                            "approval(s) expired", n)
        except Exception as e:  # noqa: BLE001 — never fatal
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("fx49 break-glass expire sweep tick raised: %s",
                           e)
        finally:
            db.close()

    _scheduler.add_job(
        _fx49_break_glass_expire_sweep_tick,
        IntervalTrigger(minutes=5),
        id="fx49_break_glass_expire_sweep",
        name="GSEC FX.49 break-glass approval expiry sweep (5-min)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # GSEC FX.69 part 2 (owner decision Q52=A) — uptime SLA sampling:
    # one sample per probe component per minute from the /health probe
    # fan-out (services/uptime.run_sampling_tick ->
    # services/platform_health_service._run_all_checks). Gated by the
    # uptime.sampling_enabled platform setting (default True). The tick
    # never raises into the scheduler.
    # ------------------------------------------------------------------
    def _fx69_uptime_sampling_tick():
        # CE build: services/uptime.py is denied+stripped (open-core/
        # manifest.yaml), so there is no SLA sampler — return early instead
        # of raising every heartbeat.
        if not _module_available("services.uptime"):
            return None  # not in this edition — no uptime SLA sampling
        _fx69 = optional_paid("services.uptime")
        if _fx69 is None:
            return
        db = _get_db()
        try:
            if not _fx69.sampling_enabled():
                logger.debug("fx69_uptime_sampling: disabled "
                             "(uptime.sampling_enabled=False)")
                return None
            summary = _fx69.run_sampling_tick(db)
            if summary:
                logger.info("fx69_uptime_sampling: %d sample(s) "
                            "recorded", summary["recorded"])
            return summary
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception("fx69_uptime_sampling tick failed: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None
        finally:
            db.close()

    _scheduler.add_job(
        _fx69_uptime_sampling_tick,
        IntervalTrigger(minutes=1),
        id="fx69_uptime_sampling",
        name="GSEC FX.69 uptime SLA sampling (1/min per component)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------
    # GSEC FX.69 part 4 [B] — monthly Enterprise uptime email: on the
    # 1st of each month at 09:00 UTC, send the previous calendar month's
    # uptime SLA report to every live Enterprise tenant's admins
    # (services/uptime.send_monthly_enterprise_reports; idempotent per
    # tenant+month via LifecycleEmailLog, never raises).
    # ------------------------------------------------------------------
    from apscheduler.triggers.cron import CronTrigger

    def _fx69_enterprise_monthly_email_tick():
        # CE build: services/uptime.py is denied+stripped (open-core/
        # manifest.yaml), so there is no uptime report to send.
        if not _module_available("services.uptime"):
            return None  # not in this edition — no uptime SLA reports
        _fx69_ent = optional_paid("services.uptime")
        if _fx69_ent is None:
            return
        db = _get_db()
        try:
            summary = _fx69_ent.send_monthly_enterprise_reports(db)
            if summary and summary.get("sent"):
                logger.info("fx69_enterprise_monthly_email: %d tenant(s) "
                            "emailed (of %d eligible)",
                            summary["sent"], summary["tenants"])
            return summary
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception("fx69_enterprise_monthly_email tick failed: %s",
                             e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None
        finally:
            db.close()

    _scheduler.add_job(
        _fx69_enterprise_monthly_email_tick,
        CronTrigger(day=1, hour=9),
        id="fx69_enterprise_monthly_email",
        name="GSEC FX.69 monthly Enterprise uptime email (1st 09:00 UTC)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GSEC FX.64 p3a [B] — weekly curated base-image rebuild ----------
    # Weekly cadence on top of the nightly dispatcher in
    # utils/base_image_rebuild.py. The gate lives inside
    # services.base_image_weekly.run_weekly_rebuild (no-op unless
    # security.base_rebuild_weekly_enabled), so the job is always
    # registered; weekday/hour come from the settings registry.
    def _base_rebuild_weekly_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.base_image_weekly")
        if _carve_m is None:
            return
        run_weekly_rebuild = getattr(_carve_m, "run_weekly_rebuild")
        _carve_m = optional_paid("utils.base_image_rebuild")
        if _carve_m is None:
            return
        enqueue_nightly_base_rebuilds = getattr(_carve_m, "enqueue_nightly_base_rebuilds")
        db = _SL()
        try:
            counters = run_weekly_rebuild(db, enqueue_nightly_base_rebuilds)
            if counters.get("triggered"):
                logger.info("base_image_weekly tick: %s", counters)
                # GO.8 (23.7.s1) — a rebuilt curated base image IS a base-image
                # change: hand the tags that landed to the on-change gate so
                # ONLY the converted variants derived from them are re-verified
                # (dark unless node_build.variant_change_gate_enabled is on).
                try:
                    from node_builder.variant_test import change_gate as _cg
                    tags = counters.get("result") or []
                    if isinstance(tags, list) and tags:
                        gate = _cg.enqueue_on_change(db, base_images=tags)
                        if gate.get("enqueued"):
                            logger.info(
                                "base_image_weekly: change gate re-queued %d "
                                "affected variant(s) for tags=%s",
                                gate["enqueued"], tags)
                except Exception as e:  # noqa: BLE001 — gate must not kill the tick
                    logger.warning("base_image_weekly change gate raised: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.exception("base_image_weekly tick raised: %s", e)
        finally:
            db.close()

    try:
        _bw_weekday = int(_psr.get_setting("security.base_rebuild_weekday") or 6)
        _bw_hour = int(_psr.get_setting("security.base_rebuild_hour_utc") or 4)
    except Exception:  # noqa: BLE001
        _bw_weekday, _bw_hour = 6, 4

    if (_module_available("services.base_image_weekly")
            and _module_available("utils.base_image_rebuild")):
        _scheduler.add_job(
            _base_rebuild_weekly_tick,
            CronTrigger(day_of_week=_bw_weekday, hour=_bw_hour),
            id="base_image_weekly_rebuild",
            name="Weekly curated base-image rebuild (GSEC FX.64 p3a)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("base_image_weekly_rebuild skipped (services.base_image_weekly / utils.base_image_rebuild not in this edition)")

    # --- GSEC FX.74 p3b [B] — weekly OFAC SDN list refresh --------------
    # The gate lives inside services.sanctions_refresh.refresh_sdn (no-op
    # unless sanctions.refresh_enabled), so the job is always registered.
    def _sdn_refresh_weekly_tick():
        from database import SessionLocal as _SL
        from services.sanctions_refresh import refresh_sdn
        db = _SL()
        try:
            outcome = refresh_sdn(db)
            if outcome.get("status") not in ("skipped",):
                logger.info("sanctions_refresh tick: %s", outcome)
        except Exception as e:  # noqa: BLE001
            logger.exception("sanctions_refresh tick raised: %s", e)
        finally:
            db.close()

    try:
        _sd_weekday = int(_psr.get_setting("sanctions.refresh_weekday") or 0)
        _sd_hour = int(_psr.get_setting("sanctions.refresh_hour_utc") or 5)
    except Exception:  # noqa: BLE001
        _sd_weekday, _sd_hour = 0, 5

    _scheduler.add_job(
        _sdn_refresh_weekly_tick,
        CronTrigger(day_of_week=_sd_weekday, hour=_sd_hour),
        id="sanctions_sdn_refresh_weekly",
        name="Weekly OFAC SDN list refresh (GSEC FX.74 p3b)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # --- GSEC FX.64 p2a [B]: nightly released-image re-scan (daily) ---
    # Re-scans the image refs MOD actually runs (services/image_rescan
    # over BuiltImageCache) with CORE/release/scan-image.sh and feeds the
    # vulnerability register via CORE/release/file_findings.py. A no-op
    # while security.rescan_enabled is False (checked inside the tick).
    def _fx64_image_rescan_daily_tick():
        from database import SessionLocal as _SL
        _fx64r = optional_paid("services.image_rescan")
        if _fx64r is None:
            return
        db = _SL()
        try:
            summary = _fx64r.run_rescan(db)
            if summary.get("scanned") or summary.get("failed"):
                logger.info("fx64_image_rescan_daily: %s", summary)
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception("fx64_image_rescan_daily tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    try:
        _fx64_hour = int(_psr.get_setting("security.rescan_hour_utc") or 3)
    except Exception:  # noqa: BLE001
        _fx64_hour = 3

    if _module_available("services.image_rescan"):
        _scheduler.add_job(
            _fx64_image_rescan_daily_tick,
            CronTrigger(hour=_fx64_hour),
            id="fx64_image_rescan_daily",
            name="Nightly released-image re-scan (GSEC FX.64 p2a)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx64_image_rescan_daily skipped (services.image_rescan not in this edition)")

    # --- GSEC FX.89 p2b (owner Q75=A) — security-contact re-confirm
    # reminder (daily 07:10 UTC). Tenants whose security contact was
    # last confirmed/updated more than security_contact.reconfirm_days
    # (default 182 = 6 months) ago get ONE reminder email to the stored
    # contact (fallback: all tenant admins), at most once per 30 days
    # (LifecycleEmailLog dedupe — no schema change). POST
    # /tenant/security-contact/confirm resets the clock.
    def _fx89_security_contact_reconfirm_tick():
        from database import SessionLocal as _SL
        db = _SL()
        try:
            from services import tenant_security_contact_service as _fx89
            sent = _fx89.remind_stale_security_contacts(db)
            if sent:
                logger.info(
                    "fx89_security_contact_reconfirm: %d reminder(s) sent",
                    sent,
                )
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception(
                "fx89_security_contact_reconfirm tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    if _module_available("services.tenant_security_contact_service"):
        _scheduler.add_job(
            _fx89_security_contact_reconfirm_tick,
            CronTrigger(hour=7, minute=10),
            id="fx89_security_contact_reconfirm",
            name="GSEC FX.89 p2b — security-contact re-confirm reminder",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx89_security_contact_reconfirm skipped (services.tenant_security_contact_service not in this edition)")

    # ------------------------------------------------------------------
    # GSEC FX.156 part 4a (owner decision Q143=B) — monthly security-alert
    # REVIEW report stored as compliance evidence: on the 1st of every
    # month (06:20 UTC) review the PREVIOUS calendar month's platform
    # security alerts (services/security_alert_review.build_monthly_review
    # — counts per rule/severity, acked vs unacked list, median time-to-
    # ack) and write one JSON evidence record per period
    # (store_monthly_review; the period IS the record key, so a re-run
    # replaces the record — never a duplicate). Pure read + file write;
    # the tick never raises into the scheduler.
    # ------------------------------------------------------------------
    def _fx156_security_alert_monthly_review_tick():
        from database import SessionLocal as _SL
        db = _SL()
        try:
            from services import security_alert_review as _fx156r
            rep = _fx156r.review_previous_month(db)
            logger.info(
                "fx156 security alert monthly review: period=%s total=%s "
                "unacked=%s median_ttack_h=%s evidence=%s",
                rep["period"], rep["total"], len(rep["unacknowledged"]),
                rep["median_time_to_ack_hours"], rep["evidence_path"])
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception(
                "fx156 security alert monthly review tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    # FX.162.p1 (owner Q157): compliance-evidence review records are paid-only.
    if is_paid_tree():
        _scheduler.add_job(
            _fx156_security_alert_monthly_review_tick,
            CronTrigger(day="1", hour=6, minute=20),  # 06:20 UTC on the 1st
            id="fx156_security_alert_monthly_review",
            name="GSEC FX.156 p4a — monthly alert-review evidence (day 1, 06:20 UTC)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx156_security_alert_monthly_review skipped "
                    "(compliance is not in this edition)")

    # ------------------------------------------------------------------
    # GSEC FX.85 p2a (owner decision Q70=A) — subprocessor change
    # NOTICE fan-out (daily 06:40 UTC). Every PUBLISHED (recorded)
    # subprocessor change is emailed once per active recipient —
    # services/subprocessor_changes.send_change_notices (recipients
    # per active_recipients: tenant subscription emails, security-
    # contact fallback). Idempotent via the append-only
    # subprocessor_notice_deliveries log: 'sent' recipients are never
    # re-mailed; 'failed' ones are retried by the next daily run.
    # Rows flagged is_ai_provider_tenant_toggled (Q70) are exempt
    # from the notice obligation. The tick never raises into the
    # scheduler (per-change failures are logged and skipped).
    # ------------------------------------------------------------------
    def _fx85_subprocessor_notice_tick():
        _fx85 = optional_paid("services.subprocessor_changes")
        if _fx85 is None:
            return
        from database import SessionLocal as _SL
        db = _SL()
        try:
            import models as _models
            change_ids = [
                cid for (cid,) in db.query(
                    _models.SubprocessorChange.id
                ).order_by(_models.SubprocessorChange.id.asc()).all()
            ]
            for cid in change_ids:
                try:
                    res = _fx85.send_change_notices(db, cid)
                    if res.get("sent") or res.get("failed"):
                        logger.info(
                            "fx85 subprocessor notice tick: change %s "
                            "sent=%s failed=%s skipped=%s",
                            cid, res.get("sent"), res.get("failed"),
                            res.get("skipped"))
                except Exception as e:  # noqa: BLE001 — one change must
                    # not stop the sweep
                    logger.warning(
                        "fx85 subprocessor notice tick: change %s "
                        "failed: %s", cid, e)
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.warning("fx85 subprocessor notice tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    if _module_available("services.subprocessor_changes"):
        _scheduler.add_job(
            _fx85_subprocessor_notice_tick,
            CronTrigger(hour=6, minute=40),  # 06:40 UTC daily
            id="fx85_subprocessor_notices",
            name="GSEC FX.85 p2a — subprocessor change notices (daily)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx85_subprocessor_notices skipped (services.subprocessor_changes not in this edition)")

    # ------------------------------------------------------------------
    # GSEC FX.82 part 3 — monthly SOC 2 evidence collection: on the 2nd
    # of every month (06:40 UTC) write one dated folder
    # <soc2.evidence_dir>/<YYYY-MM>/ of JSON evidence files (user access
    # snapshot, access-review status, security alert counts, backup/
    # restore status, recent migration change log, MFA enrolment
    # counts), each tagged with the TSC criteria it supports, plus an
    # index.json (files + criteria + sha256). Gated by the
    # soc2.evidence_enabled platform setting (default on). Pure read +
    # file write; idempotent per month; the tick never raises into the
    # scheduler.
    # ------------------------------------------------------------------
    def _fx82p3_soc2_evidence_tick():
        from database import SessionLocal as _SL
        db = _SL()
        try:
            from services import soc2_evidence as _fx82e
            index = _fx82e.run_monthly_job(db)
            if index is None:  # disabled by soc2.evidence_enabled
                logger.info(
                    "fx82p3 soc2 evidence: disabled by setting — skipping")
                return
            logger.info(
                "fx82p3 soc2 evidence: period=%s files=%d dir=%s",
                index["period"], len(index["files"]),
                index["evidence_dir"])
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception("fx82p3 soc2 evidence tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    # FX.162.p1 (owner Q157): SOC 2 evidence collection is compliance → paid-only.
    if is_paid_tree():
        _scheduler.add_job(
            _fx82p3_soc2_evidence_tick,
            CronTrigger(day="2", hour=6, minute=40),  # 06:40 UTC on the 2nd
            id="fx82p3_soc2_evidence",
            name="GSEC FX.82 p3 — monthly SOC 2 evidence collection (day 2, 06:40 UTC)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx82p3_soc2_evidence skipped (compliance is not in this edition)")

    # ------------------------------------------------------------------
    # GSEC FX.157.p2b (Q144=A host hardening) — ingest the weekly OFFLINE
    # hardening-check evidence and raise a new-failure regression alert
    # through the FX.156 engine. The host cron/timer (03:17 UTC Sundays)
    # writes one evidence JSON per host into hardening.evidence_dir; this
    # tick (03:40 UTC — after the host run) diffs each host's latest run
    # against its previous one (services/hardening_diff) and raises one
    # FX.156 alert per host run with newly failing checks, delivered
    # through the existing send_new_alert_emails path. The 5-minute
    # fx156_security_alert_rules sweep evaluates the same
    # RULE_HARDENING_REGRESSION rule; both share the evidence state file
    # (first-wins) so exactly one alert per run. Gated by
    # hardening.regression_enabled. The tick never raises into the
    # scheduler.
    # ------------------------------------------------------------------
    def _fx157p2b_hardening_ingest_tick():
        from utils.paid_import import optional_paid
        if optional_paid("services.security_alert_engine") is None:
            # FX.162.p5 — CE carve: no hardening-ingest engine.
            return
        db = _get_db()
        try:
            from services import security_alert_delivery as _fx156d
            from services import security_alert_engine as _fx157
            new_alerts = _fx157.ingest_hardening(db)
            if new_alerts:
                logger.info(
                    "fx157p2b hardening ingest: %d new alert(s): %s",
                    len(new_alerts),
                    [f"{a.rule_key}:{a.subject}" for a in new_alerts])
                sent = _fx156d.send_new_alert_emails(db, new_alerts)
                if sent:
                    logger.info(
                        "fx157p2b hardening ingest: %d alert email(s) "
                        "dispatched", sent)
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.exception("fx157p2b hardening ingest tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    # FX.162.p1 (owner Q157): hardening-regression ingest is compliance evidence → paid-only.
    if is_paid_tree():
        _scheduler.add_job(
            _fx157p2b_hardening_ingest_tick,
            CronTrigger(day_of_week="sun", hour=3, minute=40),  # after host 03:17
            id="fx157p2b_hardening_ingest",
            name="GSEC FX.157.p2b host-hardening new-failure ingest (Sun 03:40 UTC)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx157p2b_hardening_ingest skipped (compliance is not in this edition)")

    # ------------------------------------------------------------------
    # FX.98 p2a (owner decision Q84=A) — likeness-consent expiry REMINDER
    # emails. Part 1 (services/likeness_consent.py) records the consent and
    # refuses runs on an expired one; the tenant's admins must be TOLD before
    # the run starts failing. This tick emails each tenant's admins ONE email
    # per day listing that tenant's ACTIVE consents whose expires_at sits
    # inside the lead window (likeness.consent_expiry_reminder_lead_days,
    # default 14 — including consents that already lapsed inside the same
    # window and were never reported). Deduped per tenant per DAY through
    # LifecycleEmailLog's unique (tenant_id, email_key) pair — no new column,
    # no migration — so a FAILED send (no log row) retries next tick. Every
    # dispatched reminder writes its own AuditEvent row (ISO 27001 AU-2,
    # append-only; never the person name or the scope text). The gate
    # (likeness.consent_expiry_reminders_enabled) is re-read every tick.
    # 09:25 UTC daily — clear of the 09:10 FX.152 p3 key-reminder sweep.
    # ------------------------------------------------------------------
    def _fx98p2a_consent_expiry_reminder_tick():
        db = _get_db()
        try:
            from services import likeness_consent_reminders as _fx98r
            counters = _fx98r.run_consent_expiry_reminders(db)
            if counters.get("sent"):
                logger.info("fx98p2a consent expiry reminder tick: %s",
                            counters)
        except Exception as e:  # noqa: BLE001 — never kill the scheduler
            logger.warning("fx98p2a consent expiry reminder tick raised: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    if _module_available("services.likeness_consent_reminders"):
        _scheduler.add_job(
            _fx98p2a_consent_expiry_reminder_tick,
            CronTrigger(hour=9, minute=25),  # 09:25 UTC daily
            id="fx98p2a_consent_expiry_reminders",
            name="FX.98 p2a likeness-consent expiry reminder emails (daily)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx98p2a_consent_expiry_reminders skipped (services.likeness_consent_reminders not in this edition)")


    # ------------------------------------------------------------------
    # GSEC FX.144 p3a (owner decision 10-07) — EMERGENCY change-path tracking:
    # every emergency release must get its retroactive review within 24h
    # (docs/isms/09 + 09a). Every N minutes (change_mgmt.emergency_review_sweep_minutes,
    # default 15) sweep_overdue_reviews() stamps emergency rows past
    # review_due_at as review_status 'overdue' (+ the append-only
    # SECURITY_INCIDENT_CREATED audit row) and raises ONE platform-level
    # SecurityAlert per row through the FX.156 engine's dedupe table
    # (rule_key emergency_review_overdue, subject release_approval:<id>) so the
    # part-2 email/digest + ack path sees each overdue review exactly once.
    # Idempotent: a rerun with nothing new stamps and alerts nothing; a
    # soft-deleted alert is never resurrected. change_mgmt.emergency_review_alert_enabled
    # turns the ALERT half off (the stamp still happens). The tick never
    # raises into the scheduler.
    # ------------------------------------------------------------------
    def _fx144p3a_emergency_review_tick():
        # FX.144 p5 (owner 10-07, Q157=A): release approvals are compliance,
        # so paid-only — the CE carve strips services/release_approvals, so the
        # tick resolves it through utils.paid_import.optional_paid (CE = no
        # sweep, never a crash; same shape as the fx157p2b hardening tick).
        _fx144ra = optional_paid("services.release_approvals")
        if _fx144ra is None:
            return None  # FX.144 p5 — CE carve: no release-approval sweep
        from services import platform_settings as _psr_fx144
        db = _get_db()
        try:
            interval = int(_psr_fx144.get_setting(
                "change_mgmt.emergency_review_sweep_minutes") or 0)
        except Exception:  # noqa: BLE001 — setting read failure: run the sweep
            interval = 15
        if interval <= 0:
            return None  # disabled via platform setting (no restart needed)
        try:
            counters = _fx144ra.sweep_overdue_reviews(db)
            if counters.get("stamped") or counters.get("alerts"):
                logger.info(
                    "fx144p3a emergency review sweep tick: %s", counters)
        except Exception as e:  # noqa: BLE001 — never fatal
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("fx144p3a emergency review sweep tick raised: %s", e)
        finally:
            db.close()

    try:
        _fx144p3a_sweep_minutes = max(
            1, int(_psr.get_setting(
                "change_mgmt.emergency_review_sweep_minutes") or 0))
    except Exception:  # noqa: BLE001 — setting read failure: default cadence
        _fx144p3a_sweep_minutes = 15

    # FX.144 p5 (owner 10-07, Q157=A): release approvals are a COMPLIANCE
    # feature and compliance is PAID-only, so under CE the sweep job is never
    # registered (same is_paid_tree() gate FX.162.p1 uses for the 4 compliance
    # jobs — conformance_run / fx82p3_soc2_evidence / fx157p2b_hardening_ingest
    # / fx156_security_alert_monthly_review).
    if is_paid_tree():
        _scheduler.add_job(
            _fx144p3a_emergency_review_tick,
            IntervalTrigger(minutes=_fx144p3a_sweep_minutes),
            id="fx144p3a_emergency_review",
            name="GSEC FX.144 p3a emergency-review overdue sweep (stamps + one alert per row)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx144p3a_emergency_review skipped "
                    "(release approvals are not in this edition)")

    # GSEC FX.164 p4 [B] (owner Q151=A) — the DAST INGEST SCHEDULE.
    # Body at module level (_fx164p4_dast_ingest_tick, Job implementations):
    # reads the newest report pair off the read-only dast_reports volume mount
    # on the api service and merges it into the vuln register. Cadence from
    # security.dast_ingest_interval_minutes (read at scheduler start); the
    # enable switch is re-read EVERY pass, so turning it on/off needs no
    # restart.

    try:
        _fx164p4_minutes = max(
            1, int(_psr.get_setting("security.dast_ingest_interval_minutes")))
    except Exception:  # noqa: BLE001 — setting read failure: default cadence
        _fx164p4_minutes = 60

    if _module_available("services.dast_ingest"):
        _scheduler.add_job(
            _fx164p4_dast_ingest_tick,
            IntervalTrigger(minutes=_fx164p4_minutes),
            id="fx164p4_dast_ingest",
            name="GSEC FX.164 p4 DAST ingest (report pair -> vuln register)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("fx164p4_dast_ingest skipped (services.dast_ingest not in this edition)")

    # GSEC FX.120 p2c (owner decision Q107=A) — the ELEVATION AUTO-REVOKE
    # SCHEDULE. There is NO standing admin on production infrastructure, so
    # every JIT elevation (host shell / db admin / Vault admin / Docker) is
    # a time-limited window that MUST auto-revoke: this job sweeps the
    # ledger and expires a 'pending' row past its approval window
    # (security.elevation_approval_window_minutes, stamped at filing) and an
    # 'approved'/'active' row past expires_at (the TTL clock, default 2h).
    # Cadence from security.elevation_expiry_check_seconds (default 60s —
    # the guarantee is "the window ends on the clock", so the sweep runs
    # every minute, not every hour); 0 = the sweep is OFF and the job
    # no-ops, re-read EVERY pass so switching needs no restart. Every
    # expiry is audited (PRIVILEGED_ELEVATION_EXPIRED, written by
    # services/privileged_elevation.expire_lapsed) and every row that
    # leaves 'active' is handed to on_elevation_ended — the p3a/p3b hook
    # that revokes the ACTUAL capability (Vault role / issued creds /
    # Docker grant / shell session), a no-op until those parts land.
    # The tick never raises into APScheduler.
    if _module_available("services.privileged_elevation_approval"):
        try:
            _fx120_expiry_seconds = max(
                0, int(_psr.get_setting(
                    "security.elevation_expiry_check_seconds")))
        except Exception:  # noqa: BLE001 — setting read failure: default cadence
            _fx120_expiry_seconds = 60

        _scheduler.add_job(
            _fx120_elevation_expiry_tick,
            IntervalTrigger(seconds=max(1, _fx120_expiry_seconds)),
            id="fx120_elevation_expiry",
            name="GSEC FX.120 p2c elevation auto-revoke sweep (pending window + TTL clock)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # GSEC FX.120 p5b (owner decision Q107=A) — the WEEKLY ELEVATION SUMMARY
    # SEND. p2a/p2b/p2c made elevation a JIT, auto-revoking, second-person
    # ledger; p5b REPORTS it once a week to the active platform admins
    # (services/privileged_elevation_summary.build_weekly_summary +
    # send_weekly_summary): created/approved/denied/revoked/expired counts,
    # per-requester counts, the longest windows actually opened, the
    # self-approval BLOCKED attempts (p2b's refused maker-checker audit rows)
    # and the solo-mode self-approval ALERTS, plus whatever is still active.
    # Day/hour come from elevation.weekly_summary_weekday /
    # elevation.weekly_summary_hour_utc (defaults Monday 08:00 UTC — the FX.70
    # access-review slot, so the two reports are read together); the send
    # itself is gated INSIDE the tick (elevation.weekly_summary_enabled + a
    # re-read of the day/hour every pass), so moving or switching the send
    # needs no restart, and one ADMIN_SYSTEM_MAINTENANCE audit row per ISO week is the
    # dedupe record — a restart or a double-fire in the same week sends
    # nothing again. The tick never raises into APScheduler.
    if _module_available("services.privileged_elevation_summary"):
        try:
            _fx120_weekday = int(_psr.get_setting(
                "elevation.weekly_summary_weekday"))
            _fx120_hour = int(_psr.get_setting(
                "elevation.weekly_summary_hour_utc"))
        except Exception:  # noqa: BLE001 — setting read failure: Monday 08:00
            _fx120_weekday, _fx120_hour = 0, 8

        _scheduler.add_job(
            _fx120_weekly_elevation_summary_tick,
            CronTrigger(day_of_week=_fx120_weekday, hour=_fx120_hour),
            id="fx120_weekly_elevation_summary",
            name="GSEC FX.120 p5b weekly privileged-elevation summary email",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # GSEC FX.119 p3a (owner decision Q106=A) — the ON-CALL ACK WINDOW +
    # ESCALATION SCHEDULE. A page is only "handled" when the on-call person
    # ACKs it (POST /v1/oncall/pages/{id}/ack). If nobody acked inside
    # oncall.ack_window_seconds (default 900 = the owner decision's 15 min),
    # this sweep re-pages oncall.owner_topic EXACTLY ONCE, marks the page
    # 'escalated' and audits ONCALL_PAGE_ESCALATED. One hop only: an
    # escalation is never itself escalated, so two phones can never ping-pong.
    # Cadence from oncall.escalation_check_seconds (default 60s — the 15-min
    # guarantee is a MINUTE-resolution clock, so the sweep must run every
    # minute); 0 = the sweep is OFF and the job no-ops, re-read EVERY pass
    # so switching needs no restart. The tick never raises into APScheduler.
    if _module_available("services.paging_service"):
        try:
            _fx119_esc_seconds = max(
                0, int(_psr.get_setting(
                    "oncall.escalation_check_seconds")))
        except Exception:  # noqa: BLE001 — setting read failure: default cadence
            _fx119_esc_seconds = 60

        _scheduler.add_job(
            _fx119_oncall_escalation_tick,
            IntervalTrigger(seconds=max(1, _fx119_esc_seconds)),
            id="fx119_oncall_escalation",
            name="GSEC FX.119 p3a on-call escalation sweep (unacked pages -> owner topic)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # ------------------------------------------------------------------
    # GSEC FX.117 p4b (owner decision Q104=A) — the SECRET EXPIRY REMINDER
    # SCHEDULE. p4a made an expiry a RECORD (models.SecretExpiry / table
    # `secret_expiries`, one live row per secret, soft-delete only) and
    # refused an expired secret at run-time injection; p4b TELLS the tenant
    # admins before the outage bites: every secret whose live expiry is
    # within `secrets.expiry_reminder_days` (the p2 day-mark list's widest
    # mark — 14 by default) OR already past gets an in-app notification +
    # email to the tenant admins (services/secret_expiry_service.py
    # expiry_reminder_tick: models.Notification rows + services/email_send_api
    # prepare_send, the same pair services/log_streams.py uses), AT MOST ONCE
    # per `secrets.expiry_reminder_interval_hours` (default 24 h) via the live
    # row's `last_reminded_at` stamp, each send audited
    # SECRET_EXPIRY_REMINDER_SENT. The message carries the secret NAME only —
    # never the value. The job fires every hour (the dedupe is the stamp, not
    # the cadence, so an hourly heartbeat honours any interval >= 1 h and
    # catches a 1 h interval exactly); the tick never raises into APScheduler.
    # ------------------------------------------------------------------
    if _module_available("services.secret_expiry_service"):
        _scheduler.add_job(
            _fx117_secret_expiry_reminders_tick,
            IntervalTrigger(seconds=3600),  # hourly heartbeat
            id="fx117_secret_expiry_reminders",
            name="GSEC FX.117 p4b secret-expiry reminders (tenant admins, once per interval)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # --- GSEC FX.135 s4 — the crypto-mining signature sweep schedule ---
    # Body at module level (_mining_signature_sweep_tick, Job implementations).
    # s3 built the detector (services/mining_signature_detection.run_sweep) and
    # registered no job; s4 runs it every fraud.mining_sweep_interval_minutes
    # (default 10 min, read at scheduler start — same pattern as the DAST
    # ingest job above). Only the scheduler leader reaches it (leader lock
    # acquired before any job is added). fraud.mining_detection_enabled is the
    # kill switch, re-read EVERY pass, so off = a logged no-op with no restart
    # and no telemetry read. Owner decision R34.2=A: a hit holds the tenant's
    # GPU work + opens ONE abuse ticket per tenant per 24h — NO auto-suspend.
    if _module_available("services.mining_signature_detection"):
        try:
            _fx135s4_minutes = max(
                1, int(_psr.get_setting("fraud.mining_sweep_interval_minutes")))
        except Exception:  # noqa: BLE001 — setting read failure: default cadence
            _fx135s4_minutes = 10

        _scheduler.add_job(
            _mining_signature_sweep_tick,
            IntervalTrigger(minutes=_fx135s4_minutes),
            id="mining_signature_sweep",
            name="GSEC FX.135 s4 crypto-mining signature sweep (hold + ticket)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # --- 19.3.s2: field-upload session expiry sweep (every 15 min) ---
    # _expired() is lazy: an abandoned capture never asks again, so its row
    # would sit "initiated" forever. This converges the table on its own.
    # UPDATE state only — rows and chunk objects are never deleted (ISO).
    def _field_upload_expiry_sweep_tick():
        from database import SessionLocal as _SL
        _carve_m = optional_paid("services.field_upload")
        if _carve_m is None:
            return
        sweep_expired_sessions = getattr(_carve_m, "sweep_expired_sessions")
        db = _SL()
        try:
            swept = sweep_expired_sessions(db)
            if swept:
                logger.info("field_upload expiry sweep: %d session(s) expired",
                            swept)
        except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("field_upload expiry sweep raised: %s", e)
        finally:
            db.close()

    if _module_available("services.field_upload"):
        _scheduler.add_job(
            _field_upload_expiry_sweep_tick,
            IntervalTrigger(minutes=15),
            id="field_upload_expiry_sweep",
            name="Field-upload resumable session expiry sweep (19.3.s2)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("field_upload_expiry_sweep skipped (services.field_upload not in this edition)")

    # --- GSEC FX.133 p2b — the DSR deadline sweep (hourly heartbeat) ---
    # Per-STATE deadline timers (dsr.verification_window_days /
    # dsr.progress_window_days / dsr.completion_window_days) + the
    # statutory due_date (dsr.statutory_days, default 45, owner Q121=A)
    # + the reminder lead (dsr.reminder_lead_days). The sweep FLAGS
    # overdue / due-soon requests with one audit row each (the audit
    # trail is the exactly-once record) and notifies the tenant admins
    # through the existing in-app + email seam. It NEVER mutates a
    # decided request and never changes a status, so an hourly cadence
    # is idempotent at steady state.
    if _module_available("services.dsr_requests"):
        _scheduler.add_job(
            _fx133_dsr_deadline_sweep_tick,
            IntervalTrigger(seconds=3600),  # hourly heartbeat
            id="fx133_dsr_deadline_sweep",
            name="GSEC FX.133 p2b DSR deadline sweep (flag overdue/due-soon, notify admins)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.debug("fx133_dsr_deadline_sweep skipped (not in this edition)")

    # --- GSEC FX.143 p1a — the capacity-alert sweep (hourly heartbeat) ---
    # GPU-pool capacity signals (queue claim-wait + unclaimed backlog,
    # sustained GPU utilization) with every threshold a DB platform
    # setting registered in-module in services/capacity_alerts.py. The
    # measurements are REUSED from routers/admin_gpu_pool.py (the GPU-pool
    # dashboard's own panels), so the alert and the panel can never
    # disagree. A breach leaves one audit row + platform-admin
    # notifications, at most once per signal per
    # capacity_alerts.dedupe_hours — the audit trail is the dedupe record
    # (read in Python, never a JSONB op), so an hourly cadence over a
    # persistent breach writes one row per window, never one per tick.
    if _module_available("services.capacity_alerts"):
        _scheduler.add_job(
            _fx143_capacity_alert_sweep_tick,
            IntervalTrigger(seconds=3600),  # hourly heartbeat
            id="fx143_capacity_alert_sweep",
            name="GSEC FX.143 p1a capacity-alert sweep (GPU queue wait + utilization, notify admins)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.debug("fx143_capacity_alert_sweep skipped (not in this edition)")

    # --- GSEC FX.159 p3b — the contract-obligation due-date REMINDER sweep ---
    # (hourly heartbeat). The Enterprise contract-obligations tracker
    # (services/contract_obligations.py, owner Q146=A) owes the register its
    # "due-date reminders" line: an open obligation nearing its due date, or
    # past it, is pushed through the FX.156 alert-rules engine (a SecurityAlert
    # row per obligation per kind, so the Security officer's alert board, ack
    # endpoint and daily digest see contract breaches like every other signal)
    # plus the obligation's owner + tenant/platform admins through the
    # in-app + email notification seam. The AUDIT TRAIL is the exactly-once
    # record (one contract_obligation.reminder_* row per obligation per due
    # date per kind, read in Python, never a JSONB op), so an hourly cadence
    # over a persistent breach writes one row per date, never one per tick.
    if _module_available("services.contract_obligations"):
        _scheduler.add_job(
            _fx159_contract_obligation_reminders_tick,
            IntervalTrigger(seconds=3600),  # hourly heartbeat
            id="fx159_contract_obligation_reminders",
            name="GSEC FX.159 p3b contract-obligation due-date reminders (due-soon + overdue, FX.156 alerts, notify owner/admins)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.debug("fx159_contract_obligation_reminders skipped (not in this edition)")

    # --- GSEC FX.145 p2b — the yearly device-self-check sweep (weekly) ---
    # Scans the full asset inventory (workers, managers, stacks from
    # asset_inventory.py). On the first tick of each calendar year, writes
    # one audit row per device that has not yet been self-checked this
    # year. Dedup is in-Python over the audit trail (no JSONB). The
    # weekly cadence is idempotent: subsequent ticks in the same year are
    # no-ops.
    if _module_available("services.device_self_check"):
        _scheduler.add_job(
            _fx145_device_self_check_tick,
            IntervalTrigger(weeks=1),  # weekly heartbeat
            id="fx145_device_self_check",
            name="GSEC FX.145 p2b yearly device-self-check sweep (per-device audit row, exactly-once/yr)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.debug("fx145_device_self_check skipped (not in this edition)")

    # --- GSEC FX.136 p2 — the GPU-temperature-alert sweep (hourly heartbeat) ---
    # Worker telemetry already carries the reading (CORE/Worker/internal/monitor/
    # gpu.go's TemperatureC -> gpu_temperature_c, stamped into WorkerResourceTimeline
    # by /workers/heartbeat and into TaskResourceSample.gpu_temp_c by the 30s
    # resource-sample batches). This sweep only READS those rows: a GPU that has
    # held gpu_temperature_alerts.warn_c / .critical_c for
    # gpu_temperature_alerts.sustained_minutes straight leaves ONE audit row +
    # platform-admin notifications (and the owner's Q124 "thermal = log warning"
    # line), at most once per worker/level per gpu_temperature_alerts.dedupe_hours
    # — the audit trail is the dedupe record (read in Python, never a JSONB op),
    # so an hourly cadence over a persistent thermal breach writes one row per
    # window, never one per tick.
    if _module_available("services.gpu_temperature_alerts"):
        _scheduler.add_job(
            _fx136_gpu_temperature_alert_sweep_tick,
            IntervalTrigger(seconds=3600),  # hourly heartbeat
            id="fx136_gpu_temperature_alert_sweep",
            name="GSEC FX.136 p2 GPU temperature-alert sweep (sustained warn/critical, notify admins)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.debug("fx136_gpu_temperature_alert_sweep skipped (not in this edition)")

    # --- FX.62 p3a2 — the rolling worker-upgrade campaign tick (60s) ---
    # services/worker_update_rolling.py owns the campaign state machine: one
    # rollout per release, ONE worker advanced at a time, the next only
    # requested once the previous one reports healthy on the new version, and
    # a failed/timed-out step HOLDS the campaign (nothing advances until an
    # operator decides). run_upgrade_tick() is the service's own never-raising
    # entry point and is already the no-op for `worker_update.enabled` off and
    # for "nothing due" (fleet up to date / latest version unknown), so this
    # wrapper only supplies the session and keeps the scheduler alive.
    # 60s cadence: the rolling advance is a per-worker hand-off, so it has to
    # be responsive — a full rollout of a fleet must not take an hour per step.
    # The service defines no interval setting of its own (only
    # worker_update.enabled / .batch_size / .health_timeout_seconds), so the
    # cadence is the literal here.
    if _module_available("services.worker_update_rolling"):
        _scheduler.add_job(
            _fx62_rolling_upgrade_tick,
            IntervalTrigger(seconds=60),  # rolling advance must stay responsive
            id="fx62_rolling_upgrade",
            name="FX.62 p3a2 rolling worker-upgrade campaign tick (one worker at a time, hold on failure)",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.debug("fx62_rolling_upgrade skipped (not in this edition)")

    # --- sz-2.9 wiring: paused-run timeout sweep (every 5 min) ---
    # Abandoned pauses would otherwise pin the tenant's paused-run quota and
    # the operator's capacity forever. First observation only stamps the
    # `paused_seen_at` marker; a later pass cancels once the per-tenant
    # workflows.paused_run_timeout_s has elapsed from that marker. Own
    # session, off the request path (services.paused_run_limits).
    _scheduler.add_job(
        paused_run_timeout_sweep_tick,
        IntervalTrigger(minutes=5),
        id="paused_run_timeout_sweep",
        name="sz-2.9 paused-run timeout sweep (per-tenant "
             "workflows.paused_run_timeout_s)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.info("Job scheduler started with %d jobs", len(_scheduler.get_jobs()))
    for job in _scheduler.get_jobs():
        logger.info("  Scheduled: %s (next: %s)", job.name, job.next_run_time)


def _fx119_oncall_escalation_tick():
    """One scheduled on-call escalation pass (job id fx119_oncall_escalation)."""
    try:
        cadence = int(_psr.get_setting(
            "oncall.escalation_check_seconds"))
    except Exception as e:  # noqa: BLE001 — settings read failure: keep sweeping
        logger.warning("fx119_oncall_escalation tick: settings read failed: %s", e)
        cadence = 60
    if cadence <= 0:
        logger.info("fx119_oncall_escalation tick: disabled "
                    "(oncall.escalation_check_seconds 0) — no-op")
        return None
    db = _get_db()
    try:
        from services import paging_service as _fx119
        counters = _fx119.escalate_due_pages(db)
        if counters.get("due") or counters.get("escalated"):
            logger.info("fx119_oncall_escalation tick: %s", counters)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx119_oncall_escalation tick raised: %s", e)
    finally:
        db.close()
    return None


def _stack_measurement_tick():
    """One scheduled stack-measurement sweep pass (job id
    stack_measurement).

    FX.165 p1b: hoisted from a closure inside start_scheduler() to module level so
    the enqueue path (services/stack_calibration._fx165p1b_kick_measurement_sweep)
    can schedule this SAME function as a one-shot kick. Behaviour unchanged: it
    opens its own session, runs the rate-limited quiescent sweep and never raises
    into APScheduler.
    """
    from database import SessionLocal as _SL
    _sc = optional_paid("services.stack_calibration")
    if _sc is None:
        return
    db = _SL()
    try:
        _sc.run_due_measurements(db)  # self-gating: inert unless switched on
    except Exception as e:  # noqa: BLE001
        logger.exception("stack_measurement tick raised: %s", e)
    finally:
        db.close()


def _stack_measurement_tick_seconds() -> int:
    """FX.165 p1b — the measurement sweep cadence, in seconds.

    The cadence setting belongs to the paid stack_calibration layer
    (services/stack_calibration.measurement_tick_seconds); without that
    layer (Community Edition) there is no sweep and the default is returned.
    """
    _sc = optional_paid("services.stack_calibration")
    if _sc is None:
        return 30
    return _sc.measurement_tick_seconds()


def _get_db():
    """Get a fresh DB session."""
    from database import SessionLocal
    return SessionLocal()


# =========================================================================
# Job implementations
# =========================================================================


# ---------------------------------------------------------------------------
# GSEC FX.164 p4 [B] (owner Q151=A) — the DAST INGEST tick.
# p1/p2 (CORE/startup/dast/) write the ZAP baseline + ZAP api-scan JSON pair
# (and the Schemathesis JUnit XML) into the `dast_reports` volume; p3
# (services/dast_ingest) turns findings into vuln-register rows + the release
# gate. p4 closes the loop on the API side: parse the NEWEST pair the volume
# mount exposes at /zap/wrk (the p3 setting `vuln.dast_reports_dir` default —
# one path, two sides) and ingest it.
# security.dast_ingest_enabled is re-read EVERY pass (no restart to flip it)
# and defaults OFF: until the api service's read-only dast_reports mount is
# live (it lands at the restart window — mounts are fixed at container-create
# time) the API cannot see the reports, so the honest default is off and the
# tick logs a no-op. A missing/empty dir is a skip, never an error, and
# NOTHING here may raise into APScheduler — an unhandled raise in a job is
# exactly the "stops firing jobs" symptom start_scheduler_watchdog recovers
# from, and a broken ingest must not fence the other 60 jobs.
# ---------------------------------------------------------------------------
def _fx164p4_dast_ingest_tick():
    """One scheduled DAST-ingest pass (job id fx164p4_dast_ingest)."""
    db = _get_db()
    try:
        from services import dast_ingest as _fx164p4
        enabled = _psr.get_setting("security.dast_ingest_enabled")
    except Exception as e:  # noqa: BLE001 — settings read failure: stay off
        logger.warning("fx164p4_dast_ingest tick: settings read failed: %s", e)
        enabled = False
    if not enabled:
        logger.info("fx164p4_dast_ingest tick: disabled "
                    "(security.dast_ingest_enabled off) — no-op")
        return None
    try:
        result = _fx164p4.run_ingest_tick(db)
        logger.info("fx164p4_dast_ingest tick: %s", result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx164p4_dast_ingest tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.135 s4 — the CRYPTO-MINING SIGNATURE SWEEP SCHEDULE.
# s3 (services/mining_signature_detection.py) built the judge + the sweep
# (evaluate + run_sweep) and deliberately registered NO job ("the lead wires
# the scheduler job"); s4 closes the loop: run_sweep() runs on the platform
# setting fraud.mining_sweep_interval_minutes (default 10 min, cadence read
# at scheduler start like kms.health_interval_minutes / security.dast_ingest_interval_minutes). fraud.mining_detection_enabled is the kill switch and is re-read EVERY pass (no restart to flip it), so off = the wrapper no-ops without even opening telemetry. Owner decision R34.2=A: a hit HOLDS the tenant's GPU work + opens ONE abuse ticket per tenant per 24h — there is NO auto-suspend of the tenant, run_sweep's behaviour is untouched here. NOTHING may raise into APScheduler: a raise is exactly the "stops firing jobs" symptom start_scheduler_watchdog recovers from, so the sweep is wrapped and its exceptions logged.
# ---------------------------------------------------------------------------
def _mining_signature_sweep_tick():
    """One scheduled crypto-mining signature sweep (job id
    mining_signature_sweep)."""
    db = _get_db()
    try:
        from services import mining_signature_detection as _fx135s4
        if not _psr.get_setting("fraud.mining_detection_enabled", db=db):
            logger.info("mining_signature_sweep tick: disabled "
                        "(fraud.mining_detection_enabled off) — no-op")
            return None
        result = _fx135s4.run_sweep(db)
        if result.get("hits"):
            logger.warning("mining_signature_sweep tick: hits=%d runs_held=%s "
                           "tickets_opened=%s — %s",
                           len(result["hits"]), result.get("runs_held"),
                           result.get("tickets_opened"), result)
        else:
            logger.info("mining_signature_sweep tick: %s", result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("mining_signature_sweep tick raised: %s", e)
    finally:
        db.close()
    return None


def _run_remediation_canary():
    """Foolproof #5 — periodic health check on the remediation
    pipeline. Detects catalog rot, qdrant outages, embedding model
    load failures. Logs warnings on regression so the operator
    catches drift before a customer-impacting build hits it."""
    try:
        from utils.remediation_canary import run_canary
        report = run_canary()
    except Exception as e:
        logger.error("remediation canary run failed: %s", e)
        return
    if not report.get("ok"):
        logger.warning(
            "remediation canary regression: errors=%s checks=%s",
            report.get("errors"), report.get("checks"),
        )
    else:
        logger.info("remediation canary green: %s", report.get("checks"))


# EXPR 9.3 slice 4 — max byte cap for the checkpoint's saved state carried in
# a requeued task's payload (Go worker caps at 32768; we mirror the same limit).
def _validate_checkpoint_payload_cap(value):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 0 or value > 32768:
        raise ValueError(
            f"expr.resume_checkpoint_payload_max_bytes must be 0..32768, "
            f"got {value!r}")


_psr.register(
    "expr.resume_checkpoint_payload_max_bytes",
    int,
    32768,
    validator=_validate_checkpoint_payload_cap,
    group="expr",
    description="Max bytes for the checkpoint's saved state carried in a "
                "requeued task's payload (default 32768; 0 = never carry).",
)


def _stamp_requeue_resume_checkpoint(db, reaped_ids):
    """EXPR 9.3 slice 2 — hand a requeued task back its latest checkpoint.

    ``reaped_ids`` are the api_task_queue ids that were CLAIMED/RUNNING when
    the lease reaper ran; the ones it requeued now sit PENDING/ASSIGNED.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from models import (
        ApiTaskQueue, ApiTaskStatus, TaskStatus, WorkflowCheckpoint, WorkflowRun,
    )
    # The paid EXPR flags module is removed from CE by the manifest. Without it
    # phase-8 resume stamping cannot run, so skip quietly (no WARNING per tick).
    # Any other failure keeps the generic warning below.
    try:
        from services.expr_feature_flags import phase8_long_running_checkpoints_enabled
    except ImportError:
        return
    except Exception as exc:  # noqa: BLE001 — resume stamping must never block the reaper
        logger.warning("reaper resume-checkpoint stamp failed: %s", type(exc).__name__)
        return
    try:
        from services.run_resume_planner import plan_resume

        with db.begin_nested():
            for task in (db.query(ApiTaskQueue)
                         .filter(ApiTaskQueue.id.in_(
                             i for i in reaped_ids if i is not None),
                                 ApiTaskQueue.status.in_(
                                     [ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED]),
                                 ApiTaskQueue.node_run_id.is_not(None))
                         .all()):
                run = db.get(WorkflowRun, task.workflow_run_id)
                if run is None or run.deleted_at is not None:
                    continue  # the run is not RUNNING any more — nothing to resume
                if run.status != TaskStatus.RUNNING:
                    continue
                if not phase8_long_running_checkpoints_enabled(db, task.tenant_id):
                    continue
                plan = plan_resume(db, tenant_id=task.tenant_id,
                                   workflow_run_id=run.id)
                if plan.get("status") != "ok":
                    continue
                checkpoint_id = None
                for node in plan.get("resume_nodes") or []:
                    if (node.get("node_run_id") == task.node_run_id
                            and node.get("checkpoint_id") is not None):
                        checkpoint_id = node["checkpoint_id"]
                        break
                if checkpoint_id is None:
                    continue
                checkpoint = db.get(WorkflowCheckpoint, checkpoint_id)
                if checkpoint is None or checkpoint.deleted_at is not None:
                    continue

                payload = dict(task.task_payload or {})
                if "checkpoint_hydrate_url" in payload:
                    continue  # dispatch already handed it a hydrate URL
                payload["resume_checkpoint_id"] = checkpoint_id
                payload["resume_checkpoint_label"] = checkpoint.label
                payload["resume_checkpoint_progress"] = checkpoint.progress
                payload["resume_checkpoint_attempt"] = checkpoint.attempt
                # EXPR 9.3 slice 4 — carry the checkpoint's saved state
                cap = 32768
                try:
                    cap = int(_psr.get_setting(
                        "expr.resume_checkpoint_payload_max_bytes",
                        db=db, tenant_id=task.tenant_id)) or 0
                except Exception:
                    pass
                if checkpoint.payload is not None and cap > 0:
                    serialised = json.dumps(
                        checkpoint.payload, separators=(",", ":"),
                        default=str)
                    sz = len(serialised.encode("utf-8"))
                    if sz <= cap:
                        payload["resume_checkpoint_payload"] = checkpoint.payload
                    else:
                        payload["resume_checkpoint_payload_truncated"] = True
                        logger.warning(
                            "reaper requeue of task %d: checkpoint payload "
                            "%d bytes exceeds cap %d — truncating",
                            task.id, sz, cap)
                task.task_payload = payload
                flag_modified(task, "task_payload")
                logger.info("reaper requeue of task %d carries resume checkpoint %d",
                            task.id, checkpoint_id)
    except Exception as exc:  # noqa: BLE001 — resume stamping must never block the reaper
        logger.warning("reaper resume-checkpoint stamp failed: %s", type(exc).__name__)
        try:
            db.rollback()
        except Exception:
            pass


def _run_worker_maintenance():
    """Clean up stale task claims, expired assignments, offline workers."""
    db = _get_db()
    try:
        from routers.worker_tasks import (cleanup_expired_task_claims,
                                          _sweep_wedged_runs)
        from models import ApiTaskQueue, ApiTaskStatus
        _reaped = [t.id for t in db.query(ApiTaskQueue).filter(
            ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED,
                                     ApiTaskStatus.RUNNING])
        ).all()]
        cleanup_expired_task_claims(db)
        _stamp_requeue_resume_checkpoint(db, _reaped)
        # rc20-J157b — runs already wedged by a skipped/leaked dispatch cascade
        # (J157) are invisible to the reaper above (it only touches lapsed CLAIMED/
        # RUNNING rows). Same tick, same session: the sweep re-enters the normal
        # cascade entry point for at most _WEDGED_RUN_SWEEP_CAP idle runs.
        _healed = _sweep_wedged_runs(db)
        if _healed:
            logger.info("J157b wedged-run sweep: re-drove run ids %s", _healed)
        logger.debug("Worker maintenance completed")
    except Exception as e:
        logger.error("Worker maintenance failed: %s", e)
    finally:
        db.close()


def _run_worker_stall_watchdog():
    """WS4.Q21 — detect "green but silent" workers: heartbeating fine (so
    `_run_worker_maintenance`'s heartbeat-stale reaper never fires) while
    their task fetch/claim loop is wedged, so PENDING tasks they're eligible
    for sit forever. See routers.worker_tasks.detect_and_recover_stalled_fetch_loops
    for the full detection/recovery contract — this wrapper only follows
    `_run_worker_maintenance`'s db-session-per-tick pattern."""
    db = _get_db()
    try:
        from routers.worker_tasks import detect_and_recover_stalled_fetch_loops
        result = detect_and_recover_stalled_fetch_loops(db)
        if result.get("stalled") or result.get("recovered"):
            logger.info("Worker stall watchdog: %s", result)
        else:
            logger.debug("Worker stall watchdog: checked %d worker(s), none stalled", result.get("checked", 0))
    except Exception as e:
        logger.error("Worker stall watchdog failed: %s", e)
    finally:
        db.close()


def _run_host_exec_workspace_provision():
    db = _get_db()
    try:
        from services import worker_host_exec_provision as hxp
        result = hxp.provision_enabled_workers(db)
        if result.get("provisioned"):
            logger.info("Host-exec workspace auto-provision: %s", result)
        else:
            logger.debug("Host-exec workspace auto-provision: %s", result)
    except Exception as e:
        logger.error("Host-exec workspace auto-provision failed: %s", e)
    finally:
        db.close()


def _run_system_task_maintenance():
    """Reap orphaned RUNNING + drain stranded PENDING SYSTEM-queue tasks.

    SYSTEM tasks run inside the API (the Go worker ignores the SYSTEM queue).
    Nothing else consumes a PENDING SYSTEM row, so without this they strand
    forever (and their work — e.g. node-metadata scan — never runs, breaking
    dependent workflows). Startup already reaps once; this makes it periodic.
    """
    db = _get_db()
    try:
        from services.system_tasks import (
            reap_orphaned_system_tasks, drain_pending_system_tasks,
        )
        reap_orphaned_system_tasks(db)
        drain_pending_system_tasks(db)
    except Exception as e:
        logger.error("System-task maintenance failed: %s", e)
    finally:
        db.close()


def _run_malware_scan_drain():
    """FX.90 p3a (Q76=A) — run due async malware scans of large uploads.

    The upload paths queue SYSTEM-queue rows (services/malware_scan_async.py);
    this is their consumer. The module owns the verdicts, the backoff and the
    audit rows; this wrapper only follows the db-session-per-tick pattern.
    """
    db = _get_db()
    try:
        from services.malware_scan_async import drain_due_scans
        res = drain_due_scans(db)
        if res.get("ran"):
            logger.info("Async malware scan drain: %s", res)
        else:
            logger.debug("Async malware scan drain: nothing due")
    except Exception as e:
        logger.error("Async malware scan drain failed: %s", e)
    finally:
        db.close()


def _run_model_baseline_watchdog():
    """Self-heal the scheduler ONNX baseline: if object storage serves no
    LOADABLE model (every scheduler-v*.onnx corrupt/truncated), restore the
    baked-in good baseline. Idempotent no-op when a valid model is present."""
    db = _get_db()
    try:
        from services.ml_baseline import repair_baseline
        res = repair_baseline(db)
        if res.get("outcome") == "repaired":
            logger.warning("Model baseline watchdog repaired storage: %s", res)
        else:
            logger.debug("Model baseline watchdog: %s", res)
    except Exception as e:
        logger.error("Model baseline watchdog failed: %s", e)
    finally:
        db.close()


def _run_concurrent_processes_reconcile():
    """977d self-heal: reset each tenant's concurrent_processes_running to
    its actual in-flight (QUEUED+RUNNING, non-capacity-queued) run count via
    the 977c audited reconcile function. Idempotent no-op when counters are
    already correct."""
    db = _get_db()
    try:
        from utils.usage_enforcement import reconcile_concurrent_processes
        changed = reconcile_concurrent_processes(db)
        if changed:
            logger.info("concurrent_processes self-heal: reconciled %d "
                        "tenant(s): %s", len(changed), changed)
    except Exception as e:
        logger.error("concurrent_processes self-heal failed: %s", e)
    finally:
        db.close()


def _run_repo_docs_extract_sweep():
    """Wizard-rebuild §3c — purge expired repo-docs cache rows.

    Idempotent; safe to run on a tight cadence. Counts are logged
    only when non-zero so a quiet day doesn't fill the log."""
    db = _get_db()
    try:
        from node_builder.repo_docs.store import sweep_expired
        sweep_expired(db)
    except Exception as exc:
        logger.error("repo_docs_extract sweep failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _run_variant_test_pending_sweep():
    """Wizard-rebuild §9c — expire pending variant-tests whose
    30-day TTL elapsed without matching hardware ever joining
    the cluster. Expired rows flip the corresponding variant
    verification verdict to `untested_awaiting_hardware`.

    Idempotent. Safe to run hourly even when the queue is empty
    (no-op UPDATE)."""
    db = _get_db()
    try:
        from node_builder.variant_test.queue import expire_stale
        expired = expire_stale(db)
        if expired:
            logger.info(
                "variant_test_pending sweep: expired %d row(s)", expired,
            )
    except Exception as exc:
        logger.error("variant_test_pending sweep failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _run_variant_backfill_sweep():
    """Re-test / re-build accelerator variants that a registered node is
    missing but whose hardware is online now. Closes the gap where a node
    built while its hardware was offline never got its variants verified.

    Idempotent; gated by env (VARIANT_BACKFILL_ENABLED, default on). Build
    fan-out is capped per tick. No-op when nothing's missing."""
    import os as _os
    if _os.getenv("VARIANT_BACKFILL_ENABLED", "true").lower() not in ("true", "1", "yes", "on"):
        return
    # 2400LR-wiz1R — CE installs have no build-farm worker images, so every
    # candidate rebuild would hit the create_build_workflow 409 backstop and
    # spam this job's ERROR log every tick. Skip the sweep once, at INFO.
    from node_builder.build_dag.submit import build_farm_available
    if not build_farm_available()[0]:
        logger.info("variant backfill sweep skipped: build-farm images not in this edition")
        return
    db = _get_db()
    try:
        from node_builder.variant_test.backfill import run_backfill_sweep
        run_backfill_sweep(db)
    except Exception as exc:
        logger.error("variant backfill sweep failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _run_wizard_test_run_sweep():
    """workflow-agentic-execution-loop §wizard-test-run-dispatch — daily
    sweep removing sandbox test-run prefixes whose latest iteration is
    past the TTL (default 24h). Best-effort cleanup; logs failures."""
    db = _get_db()
    try:
        from services.wizard_test_run_cleanup import sweep_expired_test_runs
        cleaned = sweep_expired_test_runs(db)
        logger.info("wizard_test_run_sweep: %d session prefix(es) cleaned", cleaned)
    except Exception as e:
        logger.warning("wizard_test_run_sweep failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_node_output_cache_sweep():
    """workflow-agentic-execution-loop §workflow-node-output-cache —
    daily sweep removing node_output_cache rows whose last_accessed_at
    is older than the TTL (default 90 days). Hot rows stay because
    every cache hit bumps last_accessed_at. Best-effort."""
    db = _get_db()
    try:
        from services.node_output_cache import sweep_stale_cache
        deleted = sweep_stale_cache(db)
        logger.info("node_output_cache_sweep: %d stale rows deleted", deleted)
    except Exception as e:
        logger.warning("node_output_cache_sweep failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_wizard_downstream_edit_detector():
    """workflow-agentic-execution-loop §quality-signal — daily scan
    of recent successful wizard applies; insert `downstream_edited`
    signal for any whose current graph_json differs from the version
    that was applied. Best-effort."""
    db = _get_db()
    try:
        from services.wizard_downstream_edit_detector import detect_downstream_edits
        scanned, inserted = detect_downstream_edits(db)
        logger.info(
            "wizard_downstream_edit_detector: scanned=%d inserted=%d",
            scanned, inserted,
        )
    except Exception as e:
        logger.warning("wizard_downstream_edit_detector failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_retention_cleanup():
    """Delete expired workflow outputs past their retention period.

    GY.C15 — legal hold / preserve mode: outputs owned by a held tenant
    are excluded (supports preservation requirements, e.g. DFARS
    252.204-7012 90-day). Fail closed: if the hold lookup errors, the
    WHOLE batch is skipped rather than purging.

    GY.V7 — run-scoped holds additionally exclude the held run's
    outputs (held_run_ids is fail closed: a lookup error skips the
    whole batch)."""
    db = _get_db()
    try:
        from sqlalchemy import text
        # GY.C15 — resolve held tenants once for this batch.
        from utils.paid_import import optional_paid
        _lhold = optional_paid("services.legal_hold")
        try:
            if _lhold is None:  # FX.162.p5 — CE carve: no legal holds exist
                lh_platform_wide, lh_tenant_ids = False, set()
                lh_run_ids = set()
            else:
                lh_platform_wide, lh_tenant_ids = _lhold.held_tenants(db)
                # GY.V7 — resolve held run ids once for this batch (None =
                # lookup error = fail closed, everything held).
                lh_run_ids = _lhold.held_run_ids(db)
        except Exception as e:  # noqa: BLE001 — fail closed
            logger.error(
                "retention_cleanup: legal-hold lookup failed — skipping "
                "batch (fail closed): %s", e,
            )
            return
        if lh_platform_wide:
            logger.info(
                "retention_cleanup: skipping batch — platform-wide legal "
                "hold active; rows left in place"
            )
            return
        if lh_run_ids is None:
            logger.info(
                "retention_cleanup: skipping batch — run-hold lookup "
                "failed (fail closed); rows left in place"
            )
            return
        # Build the NOT-HELD tenant predicate (tenant id is NULL for
        # legacy rows; a NULL tenant_id is only exempt when no tenant-scoped
        # hold exists — with a tenant-scoped hold we must not touch rows we
        # cannot attribute, so require an explicit non-held tenant id).
        # GY.V7 — a run-scoped hold additionally excludes that run's
        # outputs (workflow_run_id NOT IN the held run ids).
        clauses = []
        if lh_tenant_ids:
            held_sql = ",".join(str(int(t)) for t in sorted(lh_tenant_ids))
            clauses.append(
                f"AND tenant_id IS NOT NULL AND tenant_id NOT IN ({held_sql})"
            )
            logger.info(
                "retention_cleanup: excluding %d held tenant(s) from sweep",
                len(lh_tenant_ids),
            )
        if lh_run_ids:
            runs_sql = ",".join(str(int(r)) for r in sorted(lh_run_ids))
            clauses.append(f"AND workflow_run_id NOT IN ({runs_sql})")
            logger.info(
                "retention_cleanup: excluding %d held run(s) from sweep",
                len(lh_run_ids),
            )
        not_held = " ".join(clauses)
        # Find expired outputs with their content hashes and tenant IDs
        expired = db.execute(text(f"""
            SELECT id, content_hash, tenant_id FROM workflow_node_outputs
            WHERE retention_expires_at < NOW()
            AND is_visible = true
            AND legal_hold = false
            {not_held}
        """)).fetchall()

        if not expired:
            return

        # Mark as hidden
        result = db.execute(text(f"""
            UPDATE workflow_node_outputs
            SET is_visible = false, hidden_at = NOW()
            WHERE retention_expires_at < NOW()
            AND is_visible = true
            AND legal_hold = false
            {not_held}
        """))
        count = result.rowcount

        # Write audit logs for each expired output
        try:
            from models import WorkflowOutputAuditLog
            for row in expired:
                db.add(WorkflowOutputAuditLog(
                    output_id=row[0],
                    content_hash=row[1],
                    tenant_id=row[2],
                    action="hidden",
                    performed_by_system="retention_cleanup",
                    reason="Retention period expired",
                ))
        except Exception as e:
            logger.warning("Failed to write retention audit logs: %s", e)

        db.commit()
        if count > 0:
            logger.info("Retention cleanup: marked %d expired outputs as hidden", count)
    except Exception as e:
        logger.error("Retention cleanup failed: %s", e)
        db.rollback()
    finally:
        db.close()


def _run_tenant_bucket_reconcile():
    db = _get_db()
    try:
        from services.tenant_buckets import reconcile_tenant_buckets
        reconcile_tenant_buckets(db)
    except Exception as e:
        logger.error("tenant_bucket_reconcile failed: %s", e)
    finally:
        db.close()


def _run_output_byte_retention():
    """Group Q.4 (25.6) — daily byte-purge retention sweep.

    ISO on (SaaS, or on-prem default): services.output_retention.
    apply_retention() early-returns mode='iso_on' and touches nothing —
    same pattern as _run_hard_delete_after_retention's iso_mode_on()
    early return just below. ISO off (on-prem, MOD_ISO_MODE=false):
    logs the dry-run plan first, then applies stage 1 (soft-delete
    expired unpinned intermediates) + stage 2 (purge bytes for rows past
    the purge grace period, deduped by content_hash).
    """
    db = _get_db()
    try:
        from services.output_retention import apply_retention, plan_expired_intermediates

        if iso_mode_on():
            logger.info("output_byte_retention: ISO mode on — nothing purged")
            return

        plan = plan_expired_intermediates(db)
        logger.info(
            "output_byte_retention: dry-run plan — %d candidate(s), %d byte(s)",
            plan.candidate_count, plan.total_bytes,
        )

        result = apply_retention(db, dry_run=False)
        logger.info("output_byte_retention: applied — %s", result)
    except Exception as e:
        logger.error("output_byte_retention failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _run_audit_gc():
    """GY.C5.s2 (25.6) — nightly audit-retention GC tick.

    Delegates to services.output_retention.audit_gc_tick, which reads the
    profile-driven effective audit window and records the pass as append-only
    AuditEvents. Gated by the platform setting retention.audit_gc_enabled
    (default off: the tick returns status='disabled' and touches nothing).
    Deletes nothing — audit rows are never removed (ISO 27001).
    """
    db = _get_db()
    try:
        from services.output_retention import audit_gc_tick

        result = audit_gc_tick(db)
        if result.get("status") == "disabled":
            logger.info("audit_gc: disabled via retention.audit_gc_enabled — no-op")
            return

        logger.info(
            "audit_gc: applied — %d event(s) written, window=%s months",
            result.get("events_written", 0), result.get("effective_months"),
        )
    except Exception as e:
        logger.error("audit_gc failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _run_ephemeral_input_retention():
    """GX.R4 (launch-completion-megaplan Group X) — ephemeral run-input retention.

    Two-stage, mirroring services.output_retention (the proven pattern):
      stage 1 — soft EXPIRE rows past TTL (status='expired', expired_at,
                audit tombstone);
      stage 2 — after the grace period, PURGE the storage object bytes.
    Runs in ALL modes (owner decision OQ-5(a): bytes purged even in ISO; the
    record row is NEVER deleted — ISO 27001). TTL + grace come from the DB
    platform settings (services.platform_settings). Network I/O (the byte
    purge) happens OUTSIDE any open DB transaction (see the module docstring).
    """
    db = _get_db()
    try:
        from services import ephemeral_run_inputs as _eri
        result = _eri.apply_ephemeral_retention(db)
        if result["expired"] or result["purged"]:
            logger.info("ephemeral_input_retention: applied — %s", result)
    except Exception as e:
        logger.error("ephemeral_input_retention failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


_storage_metrics_disabled_logged = False


def _run_storage_metrics():
    """Recompute tenants.storage_used_bytes — the ONLY writer of that column.

    Delegates to services.storage_measure.recompute_all (canonical definition:
    content_blobs bytes + unbacked file/output rows; see that module). The old
    main.py storage_reconcile loop (different formula) was removed in Q.1a.
    Kill switch: DISABLE_STORAGE_RECONCILE_LOOP=true skips the job.
    """
    global _storage_metrics_disabled_logged
    if os.getenv("DISABLE_STORAGE_RECONCILE_LOOP", "false").lower() == "true":
        if not _storage_metrics_disabled_logged:
            logger.info("Storage metrics job disabled via DISABLE_STORAGE_RECONCILE_LOOP")
            _storage_metrics_disabled_logged = True
        return None
    db = _get_db()
    try:
        from services.storage_measure import recompute_all
        return recompute_all(db)
    except Exception as e:
        logger.error("Storage metrics failed: %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def _run_permission_expiration_check():
    """Check for expiring permissions and send warnings."""
    db = _get_db()
    try:
        from routers.private_permissions import check_expiring_permissions
        check_expiring_permissions(db)
        logger.debug("Permission expiration check completed")
    except ImportError:
        logger.debug("Permission expiration check skipped (function not available)")
    except Exception as e:
        logger.error("Permission expiration check failed: %s", e)
    finally:
        db.close()


# =========================================================================
# tenant-resource-monitor jobs
# =========================================================================

# Tracks the high-water mark of the aggregator's prior tick so the next
# run only scans tasks completed since. On first deploy (no prior run)
# the aggregator back-fills the prior 24h.
_resource_monitor_last_aggregate_until = None  # type: ignore[assignment]


def _meter_sample_tick():
    """sz-payg.10 (payg-billing-hardening §3.2) — hourly meter-sample tick.

    Gated by the `billing.meter_sampling_enabled` platform setting
    (default True); when off, no-ops without touching the DB. Delegates the
    actual sweep to utils.meter_sampler.sample_all, which already owns the
    per-tenant try/except + commit discipline — this wrapper only owns its
    own session and never lets an exception reach APScheduler.
    """
    from services.platform_settings import get_setting
    if not get_setting("billing.meter_sampling_enabled"):
        return
    from utils.meter_sampler import sample_all
    db = _get_db()
    try:
        result = sample_all(db)
        logger.info("meter_sample tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("meter_sample tick raised: %s", e)
    finally:
        db.close()


def _payg_auto_recharge_tick():
    """PAYG 4.6 (payg-billing-hardening §5) — 5-minute auto-recharge tick.

    Opens a session the same way the neighbouring meter_sample tick does
    (`_get_db()` + close in finally) and hands it to
    services.payg_auto_recharge.sweep, which owns the on/off switch
    (`billing.auto_recharge_sweep_enabled`, default OFF), the candidate
    selection, the per-tenant prepaid_lock + commit discipline and the
    per-tenant failure alert — this wrapper only owns the session and
    never lets an exception reach APScheduler.
    """
    from services.payg_auto_recharge import sweep
    db = _get_db()
    try:
        result = sweep(db)
        logger.info("payg_auto_recharge tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("payg_auto_recharge tick raised: %s", e)
    finally:
        db.close()


def _payg_spend_alerts_tick():
    """PAYG spend-alerts 15-minute tick (sz-payg.20).

    Opens a session the same way the neighbouring payg_auto_recharge tick does
    (`_get_db()` + close in finally) and hands it to
    services.payg_spend_alerts.sweep, which owns the on/off switch
    (`billing.payg_spend_alerts_enabled`, default OFF), the postpaid candidate
    selection, the per-threshold/per-period dedupe (utils.usage_alerts'
    UsageAlertTracker) and the per-tenant error isolation — this wrapper only
    owns the session and never lets an exception reach APScheduler.
    """
    from services.payg_spend_alerts import sweep
    db = _get_db()
    try:
        result = sweep(db)
        logger.info("payg_spend_alerts tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("payg_spend_alerts tick raised: %s", e)
    finally:
        db.close()


def _node_cost_benchmark_refresh_tick():
    """Node-cost-benchmark daily refresh tick (daily 03:17, PR.5.10).

    Opens a session the same way the neighbouring payg tick does
    (`_get_db()` + close in finally) and hands it to
    ``services.node_cost_benchmark_runner.scheduled_refresh``, which owns
    the on/off switch (``pricing.node_cost_benchmark_refresh_enabled``,
    default OFF), the lookback window and min-samples settings, the
    aggregation of COMPLETED task history, and the commit.  This wrapper
    only owns the session and never lets an exception reach APScheduler.
    """
    _carve_m = optional_paid("services.node_cost_benchmark_runner")
    if _carve_m is None:
        return
    scheduled_refresh = getattr(_carve_m, "scheduled_refresh")
    db = _get_db()
    try:
        result = scheduled_refresh(db)
        logger.info("node_cost_benchmark_refresh tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("node_cost_benchmark_refresh tick raised: %s", e)
    finally:
        db.close()


def _subgraph_analyzer_tick():
    """Subgraph equivalence analyzer daily tick (03:53, expr 3.5).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.subgraph_equivalence_analyzer.scheduled_analyze``, which owns
    the on/off switch (``expr.subgraph_analyzer_job_enabled``, default OFF),
    the tenant iteration, per-tenant savepoints, and the commit.  This wrapper
    only owns the session and never lets an exception reach APScheduler.
    """
    from services.subgraph_equivalence_analyzer import scheduled_analyze
    db = _get_db()
    try:
        result = scheduled_analyze(db)
        logger.info("subgraph_equivalence_analyzer tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("subgraph_equivalence_analyzer tick raised: %s", e)
    finally:
        db.close()


def _causal_graph_refresh_tick():
    """WSIP 5.4 — nightly causal-graph refresh tick (04:17).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.causal_graph_refresh.run_causal_refresh``, which reads the
    wsip.causal_refresh_* platform settings, selects workflows with >= 50
    production runs in the last 30d, and refreshes each (bounded by
    wsip.causal_refresh_max_workflows).  The service owns no commit, so this
    wrapper commits after a clean pass.  Gated by the platform kill switch
    ``wsip.causal_refresh_enabled`` (default OFF) checked here, before the
    service does any work.  It never lets an exception reach APScheduler.
    """
    _carve_m = optional_paid("services.causal_graph_refresh")
    if _carve_m is None:
        return
    refresh_enabled = getattr(_carve_m, "refresh_enabled")
    run_causal_refresh = getattr(_carve_m, "run_causal_refresh")
    db = _get_db()
    try:
        if not refresh_enabled(db):
            logger.info("causal_graph_refresh tick: disabled (wsip.causal_refresh_enabled=False)")
            return
        result = run_causal_refresh(db)
        db.commit()
        logger.info("causal_graph_refresh tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("causal_graph_refresh tick raised: %s", e)
    finally:
        db.close()


def _wsip_outcome_sweep_tick():
    """WSIP 3.x — failure-prediction outcome sweep tick (every 15 min).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.failure_prediction_outcome_sweep.sweep_outcomes``, which reads
    the wsip.outcome_sweep_enabled kill switch (default OFF) and fills
    ``actual_outcome`` on predictions whose run is already terminal. The
    service commits itself once per tick (the node_cost_benchmark_runner
    ``scheduled_refresh`` convention), so this wrapper adds no second
    commit (an extra commit after a self-committing service would be
    harmless but redundant). Logs the result dict at info; never lets an
    exception reach APScheduler.
    """
    _carve_m = optional_paid("services.failure_prediction_outcome_sweep")
    if _carve_m is None:
        return
    sweep_outcomes = getattr(_carve_m, "sweep_outcomes")
    db = _get_db()
    try:
        result = sweep_outcomes(db)
        logger.info("failure_prediction_outcome_sweep tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("failure_prediction_outcome_sweep tick raised: %s", e)
    finally:
        db.close()


def _workflow_memory_retention_tick():
    """expr 9.11 — workflow-memory retention sweep tick (daily 03:38).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.workflow_memory_retention.sweep``, which reads the
    workflow_memory.retention_sweep_enabled kill switch (default OFF) and
    prunes expired workflow-memory rows. The service commits itself per the
    scheduled_refresh convention, so this wrapper adds no second commit.
    Logs the result dict at info; never lets an exception reach APScheduler.
    """
    from services.workflow_memory_retention import sweep as retention_sweep
    db = _get_db()
    try:
        result = retention_sweep(db)
        logger.info("workflow_memory_retention tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("workflow_memory_retention tick raised: %s", e)
    finally:
        db.close()


def _workflow_checkpoint_retention_tick():
    """expr 9.6 — checkpoint TTL soft-delete sweep tick (daily 03:52).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.workflow_checkpoint_retention.sweep``, which stamps
    ``deleted_at`` on expired workflow-checkpoint rows. The service commits
    itself, so this wrapper adds no second commit.
    Logs the result dict at info; never lets an exception reach APScheduler.
    """
    from services.workflow_checkpoint_retention import sweep as checkpoint_sweep
    db = _get_db()
    try:
        result = checkpoint_sweep(db)
        logger.info("workflow_checkpoint_retention tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("workflow_checkpoint_retention tick raised: %s", e)
    finally:
        db.close()


def _adversary_collect_sweep_tick():
    """WSIP 6.4 — adversary findings collection sweep tick (every 10 min).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.adversary_collect_sweep.sweep``, which reads the
    wsip.adversary_collect_enabled kill switch (default OFF), runs one
    bounded collection batch, commits once, and emits finding events
    post-commit itself. The service commits itself, so this wrapper adds
    no second commit. Logs the result dict at info; never lets an
    exception reach APScheduler.
    """
    _carve_m = optional_paid("services.adversary_collect_sweep")
    if _carve_m is None:
        return
    adversary_sweep = getattr(_carve_m, "sweep")
    db = _get_db()
    try:
        result = adversary_sweep(db)
        logger.info("adversary_collect_sweep tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("adversary_collect_sweep tick raised: %s", e)
    finally:
        db.close()


def _adversary_ga_tick():
    """WSIP 6.3/6.4 — adversary GA tick (every 10 min).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.adversary_ga_tick.run_ga_tick``, which reads the
    wsip.adversary_ga_enabled kill switch (default OFF) and advances at most
    ``wsip.adversary_ga_max_adversaries_per_tick`` ACTIVE pairings by one
    generation, handing each generation to ``adversary_dispatch``. The service
    commits itself (the generation bump is its own commit and
    ``dispatch_candidate`` commits per run before its spawn thread), so this
    wrapper adds no second commit. ``spawn=True`` is passed explicitly: the
    scheduler tick is the live dispatch loop tasks.md 6.4 asks for, so a
    dispatched candidate must actually run. Logs the result dict at info;
    never lets an exception reach APScheduler.
    """
    _carve_m = optional_paid("services.adversary_ga_tick")
    if _carve_m is None:
        return
    run_ga_tick = getattr(_carve_m, "run_ga_tick")
    db = _get_db()
    try:
        result = run_ga_tick(db, spawn=True)
        logger.info("adversary_ga_tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("adversary_ga_tick raised: %s", e)
    finally:
        db.close()


def _failure_predictor_training_tick():
    """WSIP 3.2/3.3 — nightly failure-predictor training (02:47).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.failure_predictor_training.run_training``, which trains the
    per-workflow models (3.2) plus the global fallback (3.3) and returns
    ``{"trained", "skipped", "errors", "global"}`` counts. The kill switch
    ``wsip.predictor_training_enabled`` (default OFF) lives inside that
    service, and the service commits itself (each per-workflow model in its
    own savepoint + commit), so this wrapper adds no second commit. Logs the
    result dict at info; never lets an exception reach APScheduler.
    """
    _carve_m = optional_paid("services.failure_predictor_training")
    if _carve_m is None:
        return
    run_training = getattr(_carve_m, "run_training")
    db = _get_db()
    try:
        result = run_training(db)
        logger.info("failure_predictor_training tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("failure_predictor_training tick raised: %s", e)
    finally:
        db.close()


def _wsip_adversary_reconcile_tick():
    """WSIP 6.1 — nightly adversary-registrar reconcile (03:31).

    Opens a session (``_get_db()`` + close in ``finally``) and calls
    ``services.adversary_workflows.reconcile_tenant`` once per LIVE tenant
    (``Tenant.deleted_at IS NULL``), committing once per tenant so one bad
    tenant cannot roll back another's work — the same posture as
    ``harbor_grant_reconcile.reconcile_tenant_repo_grants``.  No new kill
    switch: ``reconcile_tenant`` reads the existing fail-closed
    ``wsip.adversary_enabled`` flag itself (flag OFF pauses active pairings,
    it never tears down).  Logs the per-tenant result dict at info and a
    per-tenant failure by exception CLASS only; never lets an exception
    reach APScheduler.
    """
    _aw_reg = optional_paid("services.adversary_workflows")
    if _aw_reg is None:
        return
    db = _get_db()
    try:
        tenant_ids = [
            row[0] for row in db.query(models.Tenant.id).filter(
                models.Tenant.deleted_at.is_(None),
            ).all()
        ]
        for tid in tenant_ids:
            try:
                result = _aw_reg.reconcile_tenant(db, tid)
                db.commit()
                if result["created"] or result["torn_down"] or result["paused"]:
                    logger.info("wsip_adversary_reconcile tenant %s: %s", tid,
                                result)
            except Exception as exc:  # noqa: BLE001 — keep sweeping tenants
                db.rollback()
                logger.warning("wsip_adversary_reconcile tenant %s failed: %s",
                               tid, type(exc).__name__)
    except Exception as e:  # noqa: BLE001 — never kill the scheduler
        logger.exception("wsip_adversary_reconcile tick raised: %s", e)
    finally:
        db.close()


def _autonomous_canary_monitor_tick():
    """WSIP 8.2 — autonomous canary monitor tick (every 5 min).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.autonomous_canary_monitor.run_canary_monitor``, which reads the
    wsip.canary_monitor_enabled kill switch (default OFF), evaluates at most
    ``wsip.canary_monitor_max_per_tick`` open canary windows and records the
    decision. The service commits itself (once per window, before the
    post-commit promote/rollback event emits), so this wrapper adds no second
    commit. Logs the result dict at info; never lets an exception reach
    APScheduler.
    """
    _carve_m = optional_paid("services.autonomous_canary_monitor")
    if _carve_m is None:
        return
    run_canary_monitor = getattr(_carve_m, "run_canary_monitor")
    db = _get_db()
    try:
        result = run_canary_monitor(db)
        logger.info("autonomous_canary_monitor tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("autonomous_canary_monitor tick raised: %s", e)
    finally:
        db.close()


def _autonomous_operator_poll_tick():
    """WSIP 7.x — autonomous operator poll (every 15 min).

    Opens a session (`_get_db()` + close in finally) and hands it to
    ``services.autonomous_operator.run_operator_poll``, which self-commits
    (per-proposal commit). Logs the result dict at info; never lets an
    exception reach APScheduler.
    """
    _carve_m = optional_paid("services.autonomous_operator")
    if _carve_m is None:
        return
    run_operator_poll = getattr(_carve_m, "run_operator_poll")
    db = _get_db()
    try:
        result = run_operator_poll(db)
        logger.info("autonomous_operator_poll tick: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.exception("autonomous_operator_poll tick raised: %s", e)
    finally:
        db.close()


def _run_resource_monitor_aggregate():
    """tenant-resource-monitor §3.1 — hourly aggregator.

    Materialize TaskResourceAggregate rows for tasks that reached a
    terminal state since the last successful tick. Feature-flagged on
    ResourceMonitorConfig.enabled.
    """
    global _resource_monitor_last_aggregate_until
    from datetime import datetime, timezone, timedelta
    _carve_m = optional_paid("services.resource_monitor")
    if _carve_m is None:
        return
    aggregate_pending = getattr(_carve_m, "aggregate_pending")
    _get_config = getattr(_carve_m, "_get_config")

    db = _get_db()
    try:
        config = _get_config(db)
        if not config.enabled:
            logger.info("resource_monitor aggregator: skipped (enabled=false)")
            return

        now = datetime.now(timezone.utc)
        if _resource_monitor_last_aggregate_until is None:
            # First run after deploy — back-fill the prior 24h per spec §2.
            since = now - timedelta(hours=24)
        else:
            since = _resource_monitor_last_aggregate_until

        written = aggregate_pending(db, since, now)
        _resource_monitor_last_aggregate_until = now
        logger.info(
            "resource_monitor aggregator: %d new rows written (window %s → %s)",
            written, since.isoformat(), now.isoformat(),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("resource_monitor aggregator failed: %s", e)
    finally:
        db.close()


def _run_resource_monitor_leak_scan():
    """tenant-resource-monitor §3.2 — hourly leak detection scan."""
    _carve_m = optional_paid("services.resource_monitor")
    if _carve_m is None:
        return
    scan_leaks = getattr(_carve_m, "scan_leaks")
    _get_config = getattr(_carve_m, "_get_config")
    db = _get_db()
    try:
        config = _get_config(db)
        if not config.enabled:
            logger.info("resource_monitor leak scan: skipped (enabled=false)")
            return
        emitted = scan_leaks(db)
        logger.info("resource_monitor leak scan: %d new alerts emitted", len(emitted))
    except Exception as e:  # noqa: BLE001
        logger.exception("resource_monitor leak scan failed: %s", e)
    finally:
        db.close()


def _run_resource_monitor_digest():
    """tenant-resource-monitor §3.3 — daily digest email to platform admins."""
    _carve_m = optional_paid("services.resource_monitor")
    if _carve_m is None:
        return
    _get_config = getattr(_carve_m, "_get_config")
    db = _get_db()
    try:
        config = _get_config(db)
        if not config.enabled:
            logger.info("resource_monitor digest: skipped (enabled=false)")
            return
        # Defer the body composition + send to the email helper to keep
        # this orchestrator thin. The helper is added in §4.
        from utils.email_sender import send_resource_monitor_digest
        sent = send_resource_monitor_digest(db)
        logger.info("resource_monitor digest: sent to %d platform admin(s)", sent)
    except Exception as e:  # noqa: BLE001
        logger.exception("resource_monitor digest failed: %s", e)
    finally:
        db.close()


def _run_credit_renewal():
    """Process monthly credit renewals for subscriptions."""
    db = _get_db()
    try:
        from utils.credit_renewal import process_subscription_renewals
        result = process_subscription_renewals(db)
        logger.info("Credit renewal: %s", result)
    except Exception as e:
        logger.error("Credit renewal failed: %s", e)
    finally:
        db.close()


def _run_trivy_db_rebuild():
    """Rebuild the TrivyScanner container with a fresh CVE database."""
    import subprocess
    try:
        import os
        local_registry = tenant_registry()

        # Guard: skip if no local registry (prevents leading-slash "/trivy-scanner:latest" tag)
        if not local_registry:
            logger.warning("TrivyScanner rebuild skipped: no local registry configured — image_tag would be a bare leading-slash ref")
            return

        trivy_context = os.getenv("TRIVY_SCANNER_PATH", "/app/../DockerContainers/TrivyScanner")

        # Check if the Dockerfile exists
        dockerfile_path = os.path.join(trivy_context, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            # Try alternate path for containerized API
            trivy_context = "/DockerContainers/TrivyScanner"
            dockerfile_path = os.path.join(trivy_context, "Dockerfile")

        if not os.path.exists(dockerfile_path):
            logger.warning("TrivyScanner Dockerfile not found — skipping rebuild")
            return

        image_tag = f"{local_registry}/trivy-scanner:latest"

        logger.info("Rebuilding TrivyScanner with fresh CVE database...")
        result = subprocess.run(
            ["docker", "build", "-t", image_tag, "--no-cache", trivy_context],
            capture_output=True, text=True, timeout=600,
        )

        if result.returncode != 0:
            logger.error("TrivyScanner build failed: %s", result.stderr[-500:])
            return

        # Push to local registry
        push_result = subprocess.run(
            ["docker", "push", image_tag],
            capture_output=True, text=True, timeout=300,
        )

        if push_result.returncode == 0:
            logger.info("TrivyScanner rebuilt and pushed with fresh CVE database")
        else:
            logger.warning("TrivyScanner built but push failed: %s", push_result.stderr[-500:])

    except subprocess.TimeoutExpired:
        logger.error("TrivyScanner rebuild timed out")
    except Exception as e:
        logger.error("TrivyScanner rebuild failed: %s", e)


def _run_usage_alerts():
    """Send email alerts when tenants approach usage limits."""
    db = _get_db()
    try:
        from utils.usage_alerts import process_usage_alerts
        result = process_usage_alerts(db)
        logger.info("Usage alerts: %s", result)
    except Exception as e:
        logger.error("Usage alerts failed: %s", e)
    finally:
        db.close()

    # GSEC FX.41 (Q23=A) — periodic re-check of plan-downgrade overage. This
    # is the "lifts automatically / catches late overage" path that complements
    # the on-mutation gates: suspend excess seats + notify any tenant now over
    # its (downgraded) limits. Best-effort; must never break the alerts above.
    try:
        db2 = _get_db()
        try:
            _carve_m = optional_paid("services.downgrade_readonly_service")
            if _carve_m is None:
                return
            sweep_all_overage_tenants = getattr(_carve_m, "sweep_all_overage_tenants")
            fx41 = sweep_all_overage_tenants(db2)
            if fx41.get("acted_on"):
                logger.info("FX.41 overage sweep: %s", fx41)
        finally:
            db2.close()
    except Exception as e:
        logger.error("FX.41 overage sweep failed: %s", e)


def _run_gpu_pool_alerts():
    """launch-pricing-lockdown §8.4 — GPU pool queue-wait breach alerts."""
    # CE build: utils/pool_alerts.py is denied+stripped (open-core/manifest.yaml
    # `CORE/API/**/pool_alerts.py`), so the lazy import below would raise every
    # tick — return early instead.
    if not _module_available("utils.pool_alerts"):
        return None  # not in this edition — no pool-alert processor
    db = _get_db()
    try:
        from utils.pool_alerts import process_gpu_pool_alerts
        result = process_gpu_pool_alerts(db)
        # Only log breach summaries; the no-op case is too chatty at 1/min.
        if result.get("breached") or result.get("alerted"):
            logger.info("GPU pool alerts: %s", result)
    except Exception as e:
        logger.error("GPU pool alerts failed: %s", e)
    finally:
        db.close()


_mesh_breach_last_alert: dict = {}  # peer manager_id -> last alert monotonic time


def _run_egress_metering():
    """add-egress-billing — accumulate per-tenant egress (MinIO per-bucket
    traffic_sent_bytes) into tenant_egress_usage_daily + tenants.egress_used_bytes.

    Flag-gated (EGRESS_METERING_ENABLED) so it stays off until egress pricing is
    configured; runs only on the scheduler leader (one pass per cell)."""
    import os
    # GS.PSR.8 — the flag resolves through the platform-settings registry
    # (egress.metering_enabled: DB > env > False); no session in hand here
    # (the DB opens below only when the flag is on), so db=None.
    _carve_m = optional_paid("services.egress_metering")
    if _carve_m is None:
        return
    metering_enabled = getattr(_carve_m, "metering_enabled")
    if not metering_enabled():
        return
    # remove-minio-versitygw Gate 4 (D5): the MinIO Prometheus scrape only OWNS
    # the egress counter under METERING_SOURCE=minio. Default is 'apiside', where
    # egress is recorded live on presigned-GET issuance — running the scrape too
    # would double-bill. The scrape code stays here (deleted in Gate 6) and
    # callable for the overlap comparison; it just must not write when apiside.
    _carve_m = optional_paid("services.egress_metering")
    if _carve_m is None:
        return
    metering_source = getattr(_carve_m, "metering_source")
    record_egress_cycle = getattr(_carve_m, "record_egress_cycle")
    if metering_source() != "minio":
        return
    db = _get_db()
    try:
        record_egress_cycle(db)
    except Exception as e:  # noqa: BLE001
        logger.error("egress metering failed: %s", e)
    finally:
        db.close()


def _run_relay_metering():
    """tailnet-plan Group 8 — accumulate per-tenant MOD-DERP relay bytes into
    tenant_relay_usage_daily + tenants.relay_used_bytes.

    Flag-gated (relay_metering_enabled platform setting, env fallback
    RELAY_METERING_ENABLED) so it stays off until relay pricing + a
    per-account DERP metrics source are configured; runs only on the scheduler
    leader (one pass per cell)."""
    from services.platform_settings import get_setting
    if not get_setting("relay_metering_enabled"):
        return
    _carve_m = optional_paid("services.relay_metering")
    if _carve_m is None:
        return
    record_relay_cycle = getattr(_carve_m, "record_relay_cycle")
    db = _get_db()
    try:
        record_relay_cycle(db)
    except Exception as e:  # noqa: BLE001
        logger.error("relay metering failed: %s", e)
    finally:
        db.close()


def _run_mesh_metrics_refresh():
    """add-multi-region-mesh — refresh mesh/replication/stack Prometheus gauges and
    raise an RPO-breach alert (logger WARNING + audit event; the
    mesh_replication_rpo_breach gauge is the Prometheus/Alertmanager paging hook).

    Breach alerts only fire when replication is expected (MESH_REPLICATION_ENABLED),
    so a single-region / replication-not-configured bed doesn't get spammed — the
    gauge + status endpoint still reflect reality either way. Cooldown per peer."""
    import os
    import time as _t
    import models
    db = _get_db()
    try:
        from services.mesh_metrics import refresh_from_db
        summary = refresh_from_db(db)
        if os.getenv("MESH_REPLICATION_ENABLED", "false").lower() != "true":
            return
        breached = [r for r in summary.get("replication", []) if r.get("breached")]
        if not breached:
            return
        cooldown = int(os.getenv("MESH_RPO_ALERT_COOLDOWN_SECONDS", "1800"))
        now = _t.monotonic()
        for r in breached:
            peer = r.get("manager_id")
            if now - _mesh_breach_last_alert.get(peer, 0.0) < cooldown:
                continue
            _mesh_breach_last_alert[peer] = now
            logger.warning("MESH RPO BREACH: peer %s lag=%s target=%ss (%s)",
                           peer, r.get("lag_seconds"), summary.get("rpo_target"), r.get("note"))
            try:
                db.add(models.AuditEvent(
                    tenant_id=None, user_id=None, event_name="MESH_RPO_BREACH",
                    details={"peer": peer, "lag_seconds": r.get("lag_seconds"),
                             "rpo_target": summary.get("rpo_target"), "note": r.get("note")}))
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
    except Exception as e:  # noqa: BLE001
        logger.error("mesh metrics refresh failed: %s", e)
    finally:
        db.close()


def _run_grace_period_check():
    """Check for expired grace periods and suspend tenants."""
    db = _get_db()
    try:
        from utils.grace_period import check_expired_grace_periods
        result = check_expired_grace_periods(db)
        logger.info("Grace period check: %s", result)
    except Exception as e:
        logger.error("Grace period check failed: %s", e)
    finally:
        db.close()


def _run_payment_retry():
    """Retry failed invoice payments."""
    db = _get_db()
    try:
        from utils.payment_retry import process_payment_retries
        result = process_payment_retries(db)
        logger.info("Payment retry: %s", result)
    except Exception as e:
        logger.error("Payment retry failed: %s", e)
    finally:
        db.close()


def _run_assistant_session_expiry():
    """Delete AssistantSession rows whose last_active_at is older than
    the owning tenant's session_retention_hours. Cascade drops the
    AssistantMessage rows. Bounded — one SQL pass per tenant.
    """
    db = _get_db()
    try:
        from services.assistant_orchestrator import expire_sessions
        result = expire_sessions(db)
        if result.get("deleted"):
            logger.info("Assistant session expiry: %s", result)
    except Exception as e:
        logger.error("Assistant session expiry failed: %s", e)
    finally:
        db.close()


def _run_monthly_billing():
    """Roll up the previous month's unbilled usage into Invoice rows
    for every tenant and charge their saved payment method via
    Stripe. Wired in start_scheduler() to fire at 02:00 UTC on the
    1st of each month.

    billing_period=None tells the processor to compute the LAST
    calendar month (e.g. when this fires on 2026-07-01 it bills
    "2026-06"). The processor is internally idempotent — re-running
    for an already-invoiced period only invoices the rows that are
    still unbilled, so a manual re-fire is safe.
    """
    _mbp = optional_paid("utils.monthly_billing_processor")
    if _mbp is None:
        return
    _alerts = optional_paid("services.billing_alerts")
    db = _get_db()
    try:
        result = _mbp.process_monthly_billing(db=db, billing_period=None)
        logger.info("Monthly billing: %s", result)
    except Exception as e:
        logger.error("Monthly billing failed: %s", e, exc_info=True)
        if _alerts is None:
            return
        _alerts.alert_billing_failure(
            db, event_name="BILLING_JOB_FAILED",
            message=f"Monthly billing job failed: {e}",
            details={"job": "monthly_billing", "error": str(e)[:2000]},
        )
    finally:
        db.close()


def _run_weekly_overage_billing():
    """Collect this period's accrued overage weekly via interim invoices
    (weekly-overage-billing Slice 2). Wired in start_scheduler() to fire at
    03:00 UTC every Monday.

    Computes the current in-progress period internally. Idempotent across runs:
    each run only bills the delta past what prior interim runs already invoiced
    this period, and the monthly reconciliation nets the interim total out, so
    re-firing within a week is safe.
    """
    _ob = optional_paid("utils.overage_billing")
    if _ob is None:
        return
    db = _get_db()
    try:
        result = _ob.process_weekly_overage_billing(db=db)
        logger.info("Weekly overage billing: %s", result)
    except Exception as e:
        logger.error("Weekly overage billing failed: %s", e, exc_info=True)
    finally:
        db.close()


def _run_support_weekly_report():
    """GSEC FX.70 part 4 — weekly support response-time report to platform
    admins (job id fx70_support_weekly_report, Mondays 08:00 UTC).

    services/support_ticket_report.send_weekly_report builds the counts +
    median/p90 first-response minutes for the previous 7 days, emails the
    active platform admins through the FX.70 email helper pair (on-prem
    rule: own-configured email only), writes one append-only audit row and
    an admin notification. ids/numbers only — never ticket bodies. Never
    raises (the service swallows send hiccups).
    """
    db = _get_db()
    try:
        from services.support_ticket_report import send_weekly_report
        result = send_weekly_report(db)
        logger.info("Weekly support report: %s", result)
    except Exception as e:
        logger.error("Weekly support report failed: %s", e, exc_info=True)
    finally:
        db.close()


def _run_prepaid_reconcile():
    """Daily sweep re-syncing `Tenant.prepaid_balance_cents` mirrors against
    Stripe's Customer Balance (GB.PAY.12). Wired in start_scheduler() to fire
    at 03:47 UTC, SaaS-only. Per-tenant errors (e.g. a Stripe outage) are
    caught and counted inside `reconcile_prepaid_mirrors` itself, so this
    wrapper's try/except only guards against something failing before or
    after that sweep (e.g. the initial candidate-tenant query).
    """
    db = _get_db()
    try:
        from services.prepaid_reconcile import reconcile_prepaid_mirrors
        result = reconcile_prepaid_mirrors(db)
        logger.info("Prepaid balance mirror reconciliation: %s", result)
    except Exception as e:
        logger.error("Prepaid balance mirror reconciliation failed: %s", e, exc_info=True)
    finally:
        db.close()


def _run_concurrency_dispatch():
    """LDF-4 §7.3 — wake capacity-queued workflow runs when in-flight
    slots free up.

    For every tenant that has at least one capacity-queued run, count
    the in-flight (non-capacity-queued QUEUED + RUNNING) runs and
    compare to the plan's max_concurrent_processes. While slots are
    available, dispatch the OLDEST capacity-queued run by:
      1. Clearing is_capacity_queued + capacity_queue_reason.
      2. Calling start_workflow_execution() in a background thread,
         the same way the dispatch handler does.

    Idempotent + safe to fire concurrently from multiple ticks: each
    update happens inside its own transaction with row-level lock
    (FOR UPDATE SKIP LOCKED) so the same queued run can't be picked
    up twice.
    """
    db = _get_db()
    try:
        from sqlalchemy import distinct
        from models import WorkflowRun, Tenant, TaskStatus
        from utils.usage_enforcement import count_in_flight_runs

        # Find every tenant with at least one capacity-queued run.
        tenant_ids = [
            row[0] for row in db.query(distinct(WorkflowRun.tenant_id)).filter(
                WorkflowRun.is_capacity_queued.is_(True),
                WorkflowRun.status == TaskStatus.QUEUED,
            ).all()
        ]
        if not tenant_ids:
            return

        for tid in tenant_ids:
            tenant = db.query(Tenant).filter(Tenant.id == tid).first()
            if not tenant or not tenant.subscription or not tenant.subscription.plan:
                continue
            plan = tenant.subscription.plan
            max_slots = plan.max_concurrent_processes or 0
            if max_slots <= 0:
                continue

            in_flight = count_in_flight_runs(db, tid)
            slots_open = max_slots - in_flight
            if slots_open <= 0:
                continue

            # Bucket H — priority-class-aware dispatch. The legacy FIFO
            # ordering (oldest run first) is preserved as a tiebreaker
            # within priority class, but classes drain in this order:
            #   interactive > high > normal > low
            # so a website-backing interactive run that hits the capacity
            # gate doesn't sit behind a long-running batch run. CASE
            # expression keeps the SQL portable + index-friendly via the
            # priority_class index added in the migration.
            from sqlalchemy import case as sa_case
            priority_order = sa_case(
                (WorkflowRun.priority_class == "interactive", 0),
                (WorkflowRun.priority_class == "high", 1),
                (WorkflowRun.priority_class == "normal", 2),
                (WorkflowRun.priority_class == "low", 3),
                else_=2,
            )
            queued = (
                db.query(WorkflowRun)
                .filter(
                    WorkflowRun.tenant_id == tid,
                    WorkflowRun.is_capacity_queued.is_(True),
                    WorkflowRun.status == TaskStatus.QUEUED,
                )
                .order_by(priority_order.asc(), WorkflowRun.id.asc())
                .limit(slots_open)
                .with_for_update(skip_locked=True)
                .all()
            )
            for run in queued:
                run.is_capacity_queued = False
                run.capacity_queue_reason = None
                # Build the orchestrator conf the same way the dispatch
                # handler did. The persisted parameters carry everything
                # the orchestrator needs.
                conf = dict(run.parameters or {})
                conf['workflow_run_id'] = run.id
                db.commit()

                try:
                    from services.workflow_orchestrator import start_workflow_execution
                    from database import SessionLocal
                    import threading
                    threading.Thread(
                        target=start_workflow_execution,
                        args=(SessionLocal, conf, run.id),
                        daemon=True,
                    ).start()
                    logger.info(
                        "[concurrency_dispatch] tenant=%s woke run_id=%s (in_flight=%d, max=%d)",
                        tid, run.id, in_flight, max_slots,
                    )
                    in_flight += 1  # re-track for next iteration
                except Exception as exc:
                    logger.error(
                        "[concurrency_dispatch] tenant=%s run_id=%s dispatch failed: %s",
                        tid, run.id, exc,
                    )
                    # Mark failed so we don't keep retrying the same run.
                    run.status = TaskStatus.FAILED
                    run.error_message = f"Concurrency dispatch failed: {exc}"
                    db.commit()
    except Exception as e:
        logger.error("Concurrency dispatch tick failed: %s", e)
    finally:
        db.close()


def _schedule_trigger_due(trigger, now) -> bool:
    """True if a SCHEDULE trigger's cron matches `now` (within the last 60s) and it
    hasn't already fired this minute (55s guard). Pure + unit-testable."""
    from croniter import croniter
    cron_expr = (trigger.configuration or {}).get("cron", "")
    if not cron_expr:
        return False
    prev_fire = croniter(cron_expr, now).get_prev(datetime)
    if (now - prev_fire).total_seconds() > 60:
        return False
    if trigger.last_triggered_at:
        if (now - trigger.last_triggered_at).total_seconds() < 55:
            return False
    return _business_hours_ok(trigger, now)


def _business_hours_ok(trigger, now) -> bool:
    """If a temporal trigger restricts to business hours, only fire inside them (§3.2)."""
    win = (trigger.configuration or {}).get("business_hours")
    if not win:
        return True
    try:
        from services.triggers.pipeline import _in_time_window
        return _in_time_window(win)
    except Exception:  # noqa: BLE001 — a bad window must not wedge the scheduler
        return True


def _interval_trigger_due(trigger, now) -> bool:
    """True if a fixed-interval trigger's period has elapsed since its last fire (§3.1)."""
    secs = (trigger.configuration or {}).get("interval_seconds")
    if not isinstance(secs, int) or secs < 1:
        return False
    if not _business_hours_ok(trigger, now):
        return False
    last = trigger.last_triggered_at
    return last is None or (now - last).total_seconds() >= secs


def _oneshot_trigger_due(trigger, now) -> bool:
    """True if a one-shot trigger's run_at has passed and it hasn't fired yet (§3.1)."""
    if (trigger.trigger_count or 0) > 0:
        return False
    run_at = (trigger.configuration or {}).get("run_at")
    if not run_at:
        return False
    try:
        when = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now >= when


def _run_system_op_trigger(db, trigger, workflow, now, action: str, *,
                           deactivate: bool = False) -> bool:
    """Run a whitelisted platform system-op in-API (dogfood-billing Phase 1).

    Records a WorkflowRun for UI history/observability, then synchronously runs
    the registered handler in this process — no worker, no ApiTaskQueue, no DAG.
    The handler is responsible for its own per-unit commits; this wrapper only
    owns the run-record bookkeeping and trigger stats.
    """
    from models import WorkflowRun, TaskStatus
    # 1788l CE carve: services/system_ops.py is denied in CE (open-core/manifest.yaml),
    # so there is no in-API system-op handler at all — the CE behaviour with no paid
    # system-op policy is "the trigger cannot fire": no run record (a COMPLETED/FAILED
    # run row would be a lie), no stats bump, no ImportError every tick.
    try:
        from services.system_ops import run_system_op
    except ImportError:
        return False

    run = WorkflowRun(
        tenant_id=workflow.tenant_id,
        workflow_id=workflow.id,
        triggered_by_user_id=trigger.created_by_user_id,
        triggered_by_trigger_id=trigger.id,
        status=TaskStatus.RUNNING,
        run_name=f"{workflow.name} ({action})",
        purpose="system",
        started_at=now,
    )
    db.add(run)
    db.commit()  # persist the RUNNING row before the (self-committing) handler

    try:
        result = run_system_op(db, action)
        run.status = TaskStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        try:
            run.parameters = {"system_op": action, "result": result}
        except Exception:  # noqa: BLE001 — never let a non-serializable summary fail the run
            run.parameters = {"system_op": action, "result": str(result)}
        logger.info("System-op trigger %s ran '%s': %s", trigger.id, action, result)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        run.status = TaskStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = f"system-op '{action}' failed: {e}"
        logger.error("System-op trigger %s ('%s') failed: %s", trigger.id, action, e,
                     exc_info=True)

    trigger.last_triggered_at = now
    trigger.trigger_count = (trigger.trigger_count or 0) + 1
    if deactivate:
        trigger.is_active = False
    db.commit()
    return True


def _fire_schedule_trigger(db, trigger, now, *, deactivate: bool = False) -> bool:
    """Dispatch a due temporal trigger via the canonical `dispatch_trigger_run`
    (add-workflow-triggers §1.1 — was a dead `start_workflow_execution(db=…)` call
    that never existed, so scheduled runs silently never fired). Records stats; for
    one-shot triggers (`deactivate=True`) flips the trigger inactive after firing."""
    from models import Workflow, User
    from services.workflow_orchestrator import dispatch_trigger_run
    workflow = db.query(Workflow).filter(Workflow.id == trigger.workflow_id).first()
    if not workflow:
        return False

    # dogfood-billing Phase 1 — system-op workflows run an in-API handler instead
    # of dispatching a worker DAG ("workflow drives, API executes"). The trigger
    # config names a whitelisted action; no worker, no container, no graph.
    internal_action = (trigger.configuration or {}).get("internal_action")
    if internal_action:
        return _run_system_op_trigger(
            db, trigger, workflow, now, internal_action, deactivate=deactivate)

    user = db.query(User).filter(User.id == trigger.created_by_user_id).first()
    cogs = (trigger.configuration or {}).get("input_cog_ids") or []
    extra = {"input_cog_ids": cogs} if cogs else None
    logger.info("Temporal trigger %s firing: workflow %s (%s)",
                trigger.id, workflow.id, workflow.name)
    dispatch_trigger_run(
        db,
        workflow=workflow,
        triggered_by_user_id=(user.id if user else None),
        triggered_by_trigger_id=trigger.id,
        extra_inputs=extra,
    )
    trigger.last_triggered_at = now
    trigger.trigger_count = (trigger.trigger_count or 0) + 1
    if deactivate:
        trigger.is_active = False
    db.commit()
    return True


def _run_workflow_triggers():
    """Evaluate active temporal triggers (cron / interval / one-shot) and dispatch
    those whose schedule matches now.

    GT.C2 — when dispatch is paused (drain mode) this skips the tick WITHOUT
    touching the triggers' next-fire state, so a missed cron fires ONCE (not N
    times) the moment dispatch resumes. Nothing is advanced or deactivated."""
    try:
        from services import platform_settings as _psr_wf
        if _psr_wf.get_setting("dispatch.paused"):
            return
    except Exception:  # noqa: BLE001 — a settings read failure means dispatch on
        pass

    db = _get_db()
    try:
        from models import WorkflowTrigger, WorkflowTriggerType

        now = datetime.now(timezone.utc)
        TT = WorkflowTriggerType
        triggers = db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type.in_([TT.SCHEDULE, TT.INTERVAL, TT.ONE_SHOT]),
            WorkflowTrigger.is_active == True,
            WorkflowTrigger.deleted_at.is_(None),
        ).all()

        for trigger in triggers:
            try:
                tt = trigger.trigger_type
                if tt == TT.SCHEDULE and _schedule_trigger_due(trigger, now):
                    _fire_schedule_trigger(db, trigger, now)
                elif tt == TT.INTERVAL and _interval_trigger_due(trigger, now):
                    _fire_schedule_trigger(db, trigger, now)
                elif tt == TT.ONE_SHOT and _oneshot_trigger_due(trigger, now):
                    _fire_schedule_trigger(db, trigger, now, deactivate=True)
            except Exception as e:
                db.rollback()
                logger.error("Trigger %s evaluation failed: %s", trigger.id, e)
                continue

    except ImportError:
        logger.debug("croniter not installed — schedule triggers disabled. pip install croniter")
    except Exception as e:
        logger.error("Workflow trigger evaluation failed: %s", e)
    finally:
        db.close()


def _run_system_threshold_triggers():
    """Evaluate SYSTEM_THRESHOLD triggers (queue depth, workers online, error rate)
    with hysteresis (add-workflow-triggers §6.4/§6.5)."""
    db = _get_db()
    try:
        from services.triggers.watchers import evaluate_system_threshold_triggers
        evaluate_system_threshold_triggers(db)
    except Exception as e:
        logger.error("system_threshold trigger evaluation failed: %s", e)
    finally:
        db.close()


def _run_poll_triggers():
    """Evaluate POLL triggers — HTTP (conditional GET) + RSS (last-seen) polling
    with SSRF guard (add-workflow-triggers §8.3)."""
    db = _get_db()
    try:
        from services.triggers.streaming import evaluate_poll_triggers
        evaluate_poll_triggers(db)
    except Exception as e:
        logger.error("poll trigger evaluation failed: %s", e)
    finally:
        db.close()


def _run_calendar_triggers():
    """Evaluate CALENDAR triggers — calendar-poll date matching (EXPR 13.5).

    The evaluator lives in services/triggers/calendar_poll.py; the scheduler is
    the only caller, so without this job calendar triggers never fire. The log
    line carries the exception TYPE only: provider errors can echo tokens."""
    db = _get_db()
    try:
        from services.triggers.calendar_poll import evaluate_calendar_triggers
        evaluate_calendar_triggers(db)
    except Exception as e:
        logger.error("calendar trigger evaluation failed: %s", type(e).__name__)
    finally:
        db.close()


def iso_mode_on() -> bool:
    """ISO mode: records are soft-deleted and never purged (owner rule, 2026-09-27).
    Always on for SaaS. On-prem it defaults on; a customer install that does not
    run ISO on its own manager can opt out with MOD_ISO_MODE=false (Group Q 25.6)."""
    import os
    if os.getenv("DEPLOYMENT_MODE", "").lower() == "saas":
        return True
    return os.getenv("MOD_ISO_MODE", "true").lower() not in ("false", "0", "no", "off")


def _run_retire_archived_tenants():
    """Hard-delete tenants whose hard_delete_after date has passed.

    Order of destruction per tenant:
      1. Harbor project (removes all repos + scan history)
      2. MinIO bucket (images + any content in tenant-{id}-files)
      3. Vault tenant secrets (secret/data/tenants/{id}/*)
      4. Audit event TENANT_HARD_DELETED (stamped BEFORE DB row delete
         so the event survives with a foreign-key placeholder)
      5. DB row (flip deleted_at to well past, drop hard_delete_after)
         — we keep the row itself for referential integrity on old
         audit entries but fully scrub PII.

    Any step failing aborts THAT tenant (other tenants continue).
    Designed to be idempotent: a partial previous run's tenant gets
    the remaining steps on the next pass.
    """
    from utils.paid_import import optional_paid
    import os
    if iso_mode_on():
        logger.info("retire_archived_tenants: ISO mode on — archived tenants are kept, nothing purged")
        return
    # GS.PSR.8 — the toggle resolves through the platform-settings
    # registry (harbor.tenant_harbor_enabled: DB > env > True). The
    # import is in-function (like the `import models` below) because
    # this function's source is exec'd in isolation by
    # tests/test_tenant_harbor.py; the import resolves there too, and
    # the helper honours the same ENABLE_TENANT_HARBOR env via the
    # registry's env fallback.
    _carve_m = optional_paid("utils.tenant_harbor_client")
    if _carve_m is None:
        return
    tenant_harbor_enabled = getattr(_carve_m, "tenant_harbor_enabled")
    if not tenant_harbor_enabled():
        logger.info("retire_archived_tenants: ENABLE_TENANT_HARBOR=false, skipping")
        return

    db = _get_db()
    try:
        import models
        candidates = (
            db.query(models.Tenant)
            .filter(models.Tenant.hard_delete_after.isnot(None))
            .filter(models.Tenant.hard_delete_after <= datetime.now(timezone.utc))
            .all()
        )
        if not candidates:
            logger.debug("retire_archived_tenants: nothing to purge")
            return

        logger.info("retire_archived_tenants: %d tenant(s) eligible", len(candidates))
        _lhold = optional_paid("services.legal_hold")
        for tenant in candidates:
            if tenant.id == 1:
                logger.warning(
                    "retire_archived_tenants: refusing to purge platform tenant id=1"
                )
                continue
            # GY.C15 — legal hold / preserve mode: a held tenant's data
            # must NOT be retired (is_held fails closed: a lookup error
            # counts as held). Supports preservation requirements (e.g.
            # DFARS 252.204-7012 90-day).
            try:
                if _lhold is not None and _lhold.is_held(db,
                                                         tenant_id=tenant.id):
                    logger.info(
                        "retire_archived_tenants: skipping tenant %s — "
                        "legal hold active; data preserved", tenant.id,
                    )
                    continue
            except Exception as e:  # noqa: BLE001 — fail closed
                logger.error(
                    "retire_archived_tenants: hold lookup failed for tenant %s — "
                    "failing closed (skipping retire): %s", tenant.id, e,
                )
                continue
            try:
                _retire_one_tenant(db, tenant)
            except Exception as e:
                logger.error("retire_archived_tenants: tenant %s purge failed: %s", tenant.id, e)
                # Don't re-raise — keep going to the next tenant.
    finally:
        db.close()


def _erase_stack_secrets(db, tid: int, token: str, delete_kv_v2) -> tuple:
    """Erasure: wipe the tenant's Vault-backed stack secret VALUES
    (``tenants/<tid>/stacks/<stack>/vars/<name>``). Returns (deleted, errors);
    errors are ``stacks/<stack-name-or-row-id>: <ExcName>`` — class names only,
    never a value, never exception text.

    ANS-Q15c-stacks. A stack secret value lives ONLY in Vault; the DB keeps an
    append/soft-delete mirror (``stack_secret_index``) and the compose
    declarations. The API's AppRole cannot LIST a Vault prefix, so every path
    is enumerated from DB rows BEFORE any delete, from two sources (union,
    deduped, order preserved): (a) ALL index rows for the tenant, soft-deleted
    ones included — a rotation stamps ``deleted_at`` on the prior live row and
    the prior path is still alive in KV history; and (b) the tenant's
    ``PersistentStack`` declarations (soft-deleted stacks keep their rows),
    secret_env/secret_file entries whose ``vault:`` ref is absent get the
    deterministic computed path from ``services.stack_variables._vault_path``
    (single format). (b) matters because ``upsert_secret_index`` is best-effort
    and can miss a path. Paths re-pointed OUTSIDE ``tenants/<tid>/stacks/``
    (a ``secrets/`` ref, another tenant, ``external-farms/``) or carrying a
    ``..``/empty segment, a percent sign (``%2e%2e``/``%2f`` decode back into
    traversal once ``delete_kv_v2`` interpolates the path raw into the Vault
    URL) or a backslash are SKIPPED, never deleted — those subtrees are swept
    by the existing ``secrets/`` and FX-EXTFARM-ERASURE loops. A non-string
    ``vault:`` ref (a compose spec that carried a list/int/dict) is refused as
    a ``TypeError`` error entry and one malformed stack never stops the others;
    both DB queries run inside ``db.begin_nested()`` so a DB error leaves the
    caller's transaction usable. NO DB writes:
    the index rows hold no secret material and ISO forbids hard-deleting them.
    ``services.stack_variables`` is imported function-locally (CE paid-module
    convention); an ImportError is recorded and the index-only sweep still runs.
    """
    tid_i = int(tid)
    prefix = f"tenants/{tid_i}/stacks/"
    deleted = 0
    errors: list = []
    targets: list = []
    seen: set = set()

    def _add(path, label) -> None:
        """Prefix guard: only the tenant's own stacks subtree, no traversal.

        A non-string ``vault`` ref (a compose spec that carried a list/int/dict)
        is refused as a ``TypeError`` error entry — never an exception that
        aborts the sweep. Percent-encoded traversal (``%2e%2e``, ``%2f``) and
        backslashes are refused outright: ``delete_kv_v2`` interpolates the path
        raw into the Vault URL, so the server would decode them back into
        ``..``/``/`` after the ``..``/empty-segment check passed.
        """
        if not isinstance(path, str):
            errors.append(f"stacks/{label}: TypeError")
            return
        if "%" in path or "\\" in path:
            return
        if not path or ".." in path.split("/") or "" in path.split("/"):
            return
        if not path.startswith(prefix):
            return
        if path in seen:
            return
        seen.add(path)
        targets.append((label, path))

    try:
        with db.begin_nested():
            index_rows = (
                db.query(models.StackSecretIndex)
                .filter(models.StackSecretIndex.tenant_id == tid_i)
                .order_by(models.StackSecretIndex.id)
                .all()
            )
    except Exception as e:
        index_rows = []
        errors.append(f"stacks/index: {type(e).__name__}")
    for row in index_rows:
        _add(row.path, str(row.stack_name or row.id))

    try:
        from services import stack_variables as _sv
    except ImportError:
        _sv = None
        errors.append("stacks/declared: ImportError")

    try:
        with db.begin_nested():
            stacks = (
                db.query(models.PersistentStack)
                .filter(models.PersistentStack.tenant_id == tid_i)
                .all()
            )
    except Exception as e:
        stacks = []
        errors.append(f"stacks/declared: {type(e).__name__}")
    for stack in stacks:
        try:
            spec = stack.compose_spec or {}
            xmod = spec.get("x-mod") if isinstance(spec, dict) else None
            declared = xmod.get("variables") if isinstance(xmod, dict) else None
            if not isinstance(declared, dict):
                continue
            for name, entry in declared.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("kind") not in ("secret_env", "secret_file"):
                    continue
                path = entry.get("vault") or ""
                if not path and _sv is not None:
                    path = _sv._vault_path(tid_i, stack.name, name)
                _add(path, stack.name)
        except Exception as e:
            errors.append(f"stacks/{stack.name or 'unknown'}: {type(e).__name__}")
            continue

    for label, path in targets:
        try:
            delete_kv_v2(path, token=token)
            deleted += 1
        except Exception as e:
            errors.append(f"stacks/{label}: {type(e).__name__}")
    return (deleted, errors)


def _erase_scim_tokens(db, tid: int, token: str, delete_kv_v2) -> tuple:
    """Erasure: wipe tenants/<tid>/scim/<id> (plaintext SCIM bearer tokens) and
    soft-revoke the model-less scim_tokens rows. Returns (deleted, errors).
    Never logs/returns a token; errors are 'scim/<id>: <ExcName>'.

    ANS-Q15c-scim. ``scim_tokens`` is model-less (no ``models.ScimToken``) and
    belongs to the paid SCIM build, so this sweep uses raw SQL only — scheduler.py
    ships in CE and must never import ``scim.*``. In a CE database the table may
    not exist: both statements run inside their own SAVEPOINT so a missing table
    rolls back only the savepoint and leaves the caller's transaction usable
    (silent no-op). The API's AppRole cannot list a Vault prefix, so every path
    is enumerated from DB rows BEFORE any row is touched; revoked rows are swept
    too (soft-revoke does not delete the Vault plaintext). Stored ``vault_path``
    values are used only when they sit inside the tenant's own scim prefix
    (plural ``tenants/``, or the legacy singular ``tenant/`` form the migration
    docstring documents) and contain no ``..`` segment — anything else is
    skipped, so nothing outside ``tenants/<tid>/`` is ever deleted. Rows are
    soft-revoked (``revoked_at = now()``), never DELETEd (ISO 27001: append/soft
    only) — a replay of the ledger must not leave a hash row that can still
    match an IdP's token.
    """
    from sqlalchemy import text

    tid_i = int(tid)
    prefix = f"tenants/{tid_i}/scim/"
    legacy_prefix = f"tenant/{tid_i}/scim/"
    deleted = 0
    errors: list = []

    rows = []
    try:
        with db.begin_nested():
            rows = [
                (int(r[0]), (r[1] or ""))
                for r in db.execute(
                    text(
                        "SELECT id, vault_path FROM scim_tokens "
                        "WHERE tenant_id = :tid"
                    ).bindparams(tid=tid_i)
                )
            ]
    except Exception as e:
        # No table (CE build / not yet migrated): nothing to erase.
        logger.debug("scim erasure: scim_tokens table unavailable: %s", type(e).__name__)
        return (0, [])

    targets: list = []
    seen: set = set()
    for token_id, stored in rows:
        for cand in (f"{prefix}{token_id}", stored):
            if not cand or ".." in cand.split("/"):
                continue
            if not (cand.startswith(prefix) or cand.startswith(legacy_prefix)):
                continue
            if cand in seen:
                continue
            seen.add(cand)
            targets.append((token_id, cand))
    for token_id, path in targets:
        try:
            delete_kv_v2(path, token=token)
            deleted += 1
        except Exception as e:
            # Class name only: never a token, never the message, path text
            # limited to the row's own id.
            errors.append(f"scim/{token_id}: {type(e).__name__}")

    try:
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE scim_tokens SET revoked_at = :now "
                    "WHERE tenant_id = :tid AND revoked_at IS NULL"
                ).bindparams(now=datetime.now(timezone.utc), tid=tid_i)
            )
    except Exception as e:
        logger.debug("scim erasure: soft-revoke skipped for tenant %s: %s", tid_i, type(e).__name__)
    return (deleted, errors)


def _erase_he_keys(db, tid: int, token: str, delete_kv_v2) -> tuple:
    """Erasure: wipe the tenant's HE-key custody paths ``he-keys/<tid>/<wid>``.
    Returns (deleted, errors); errors are ``he-keys/<wid>: <ExcName>`` — class
    names only, never key material, never exception text.

    ANS-Q15c-he-keys. A custodial HE key lives ONLY in Vault at
    ``he-keys/<tenant_id>/<workflow_id>`` (``services/he_key_custody.py::_vault_path``
    — the single path format, reused here through a function-local import so the
    format can never drift); the DB mirror row (``Secret(kind='he_key')``, name
    ``he-key-<wid>``) carries only the public fingerprint and an EMPTY
    ``encrypted_value``, so erasing rows erases nothing — the Vault path survives
    every existing sweep (headline gap). The API's AppRole cannot LIST a Vault
    prefix, so every path is enumerated from DB rows BEFORE any delete, from two
    sources (union, deduped, order preserved): (a) the tenant's ``Workflow`` rows
    — soft-deleted included, a soft-deleted workflow keeps its HE key alive in
    Vault — and (b) the tenant's ``Secret(kind='he_key')`` rows, whose
    ``he-key-<int>`` name names the workflow. A name that does not yield a
    positive int workflow id is SKIPPED, never deleted (mirrors
    ``he_key_custody._positive_id``: the path can never traverse), and every
    candidate path is re-checked to start with ``he-keys/<tid>/`` so a re-pointed
    row can never reach another tenant's subtree. NO DB writes at all: the
    Secret rows and Workflow rows stay (ISO append/soft-only) — the custody
    guard (``_he_secret_rows`` live-only filter, ``find_he_key_by_fingerprint``
    tenant-scoping) keeps working because nothing here touches a row; the Vault
    delete is what erases the key. Both DB queries run inside their own
    ``db.begin_nested()`` savepoint so a DB error rolls back only the savepoint,
    is recorded as ``he-keys/workflows: <ExcName>`` / ``he-keys/rows: <ExcName>``,
    and leaves the other source and the other sweeps running.
    """
    # CE build: services/he_key_custody.py is denied+stripped (open-core/
    # manifest.yaml), so a literal import here would raise every sweep. Fetch it
    # at call time; absent == nothing was ever stored here, so skip cleanly
    # (one info line, the other sweeps keep running) rather than crash the purge.
    from utils.paid_import import optional_paid

    _hec = optional_paid("services.he_key_custody")
    if _hec is None:
        logger.info(
            "HE key custody sweep skipped: services.he_key_custody is not in "
            "this edition"
        )
        return (0, ["he-keys: not in this edition"])

    tid_i = int(tid)
    prefix = f"{_hec._VAULT_PATH_PREFIX}/{tid_i}/"
    deleted = 0
    errors: list = []
    targets: list = []
    seen: set = set()

    def _add(wf_id) -> None:
        """Positive-int workflow id + own-tenant prefix guard, else skip."""
        if isinstance(wf_id, bool) or not isinstance(wf_id, int) or wf_id <= 0:
            return
        path = _hec._vault_path(tid_i, wf_id)
        if not path.startswith(prefix) or ".." in path.split("/") or "" in path.split("/"):
            return
        if path in seen:
            return
        seen.add(path)
        targets.append((wf_id, path))

    try:
        with db.begin_nested():
            workflows = (
                db.query(models.Workflow)
                .filter(models.Workflow.tenant_id == tid_i)
                .all()
            )
    except Exception as e:
        workflows = []
        errors.append(f"he-keys/workflows: {type(e).__name__}")
    for wf in workflows:
        _add(wf.id)

    try:
        with db.begin_nested():
            rows = (
                db.query(models.Secret)
                .filter(
                    models.Secret.tenant_id == tid_i,
                    models.Secret.kind == "he_key",
                )
                .all()
            )
    except Exception as e:
        rows = []
        errors.append(f"he-keys/rows: {type(e).__name__}")
    for row in rows:
        name = (row.name or "").strip()
        if not name.startswith("he-key-"):
            continue
        suffix = name[len("he-key-"):]
        if not suffix.isdigit():
            continue
        _add(int(suffix))

    for wf_id, path in targets:
        try:
            delete_kv_v2(path, token=token)
            deleted += 1
        except Exception as e:
            errors.append(f"he-keys/{wf_id}: {type(e).__name__}")
    return (deleted, errors)


def _retire_one_tenant(db, tenant) -> None:
    """Destroy one tenant's external resources, then clear the DB row."""
    tid = tenant.id
    log = themed_logger(__name__, 'scheduler')

    # 1. Harbor project
    try:
        from utils.tenant_harbor_client import TenantHarborClient
        TenantHarborClient().delete_project(tid)
        log.info("  Harbor project for tenant %s deleted", tid)
    except Exception as e:
        log.warning("  Harbor delete_project failed for tenant %s: %s (continuing)", tid, e)

    # 2. MinIO bucket (best effort; bucket may already be gone or never existed)
    try:
        from utils.minio_client import MinioClient, get_tenant_bucket_name
        bucket = get_tenant_bucket_name(tid, db)
        mc = MinioClient(tenant_id=tid)
        # Empty the bucket first — MinIO refuses to delete non-empty
        # buckets. Listing + removing objects is enough.
        try:
            for obj in mc.client.list_objects(bucket, recursive=True):
                mc.client.remove_object(bucket, obj.object_name)
        except Exception as inner:
            log.warning("  MinIO bucket %s empty-on-delete had issues: %s", bucket, inner)
        mc.client.remove_bucket(bucket)
        log.info("  MinIO bucket %s deleted", bucket)
    except Exception as e:
        log.warning("  MinIO bucket delete failed for tenant %s: %s (continuing)", tid, e)

    # 3. Vault tenant secrets
    try:
        import os
        from utils.vault_client import approle_login, delete_kv_v2
        role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
        secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
        if role_id and secret_id:
            token = approle_login(role_id=role_id, secret_id=secret_id)
            # Wipe every secret under the tenant's subtree.
            for subpath in ("tenant_harbor",):
                try:
                    delete_kv_v2(f"tenants/{tid}/{subpath}", token=token)
                except Exception as inner:
                    log.warning("  Vault wipe of tenants/%s/%s failed: %s", tid, subpath, inner)
            # FX-EXTFARM-ERASURE: farm credentials live at
            # tenants/<tid>/external-farms/<fid>. The rows CASCADE-delete with the
            # tenant, so enumerate them BEFORE the row wipe or the secret survives.
            # Best-effort per farm; the path is ints-only and nothing here logs a
            # token or a credential.
            try:
                from services import external_farms as _efarm
                for farm in (
                    db.query(models.ExternalFarmConnection)
                    .filter(models.ExternalFarmConnection.tenant_id == tid)
                    .all()
                ):
                    try:
                        delete_kv_v2(_efarm._farm_vault_path(tid, farm.id), token=token)
                    except Exception as inner:
                        log.warning("  Vault wipe of external farm %s/%s failed: %s", tid, farm.id, inner)
            except Exception as e:
                log.warning("  External farm Vault sweep failed for tenant %s: %s (continuing)", tid, e)
            # ANS-Q15c-secrets: tenant secret VALUES live in Vault at
            # tenants/<tid>/secrets/<name> (writer: crud/secret_cud.py::_vault_tenant_secret_path,
            # which guards the name with (secret_name or "").strip()). The rows
            # CASCADE-delete with the tenant stub, so enumerate them BEFORE the
            # row wipe or the value survives. Best-effort per path — one failure
            # must not abort the erasure. Errors carry class names only: a Vault
            # error message can carry the token.
            try:
                for s in (
                    db.query(models.Secret)
                    .filter(models.Secret.tenant_id == tid)
                    .all()
                ):
                    try:
                        delete_kv_v2(
                            f"tenants/{tid}/secrets/{(s.name or '').strip()}",
                            token=token,
                        )
                    except Exception as inner:
                        log.warning(
                            "  Vault wipe of tenants/%s/secrets/%s failed: %s",
                            tid,
                            s.name,
                            type(inner).__name__,
                        )
            except Exception as e:
                log.warning(
                    "  Secret Vault sweep failed for tenant %s: %s (continuing)",
                    tid,
                    type(e).__name__,
                )
            # ANS-Q15c-scim: SCIM bearer tokens are stored PLAINTEXT in Vault at
            # tenants/<tid>/scim/<id>; the model-less scim_tokens rows do not
            # cascade with the tenant stub, so sweep them here (before the audit
            # event and the row scrub, while the tenant is still intact).
            try:
                _n, _errs = _erase_scim_tokens(db, tid, token, delete_kv_v2)
                for _e in _errs:
                    log.warning("  SCIM token wipe for tenant %s: %s", tid, _e)
            except Exception as e:
                log.warning("  SCIM token sweep failed for tenant %s: %s (continuing)", tid, type(e).__name__)
            # ANS-Q15c-stacks: stack secret VALUES live only in Vault under
            # tenants/<tid>/stacks/; the index rows and the compose declarations
            # keep no secret material, so nothing cascades with the tenant stub.
            # Sweep here (before the audit event and the row scrub, while the
            # rows are still readable). Errors carry ids + class names only.
            try:
                _n, _errs = _erase_stack_secrets(db, tid, token, delete_kv_v2)
                if _n:
                    log.info("  Stack secret values for tenant %s purged (%s)", tid, _n)
                for _e in _errs:
                    log.warning("  Stack secret wipe for tenant %s: %s", tid, _e)
            except Exception as e:
                log.warning("  Stack secret sweep failed for tenant %s: %s (continuing)", tid, type(e).__name__)
            # ANS-Q15c-he-keys: custodial HE key material lives ONLY in Vault at
            # he-keys/<tid>/<wid> (services/he_key_custody.py::_vault_path); the
            # Secret(kind='he_key') mirror row carries no key material, so the
            # secrets/ loop above erases nothing here. Sweep before the audit
            # event and the row scrub (same audit-event position rule).
            try:
                _n, _errs = _erase_he_keys(db, tid, token, delete_kv_v2)
                if _n:
                    log.info("  HE key custody paths for tenant %s purged (%s)", tid, _n)
                for _e in _errs:
                    log.warning("  HE key wipe for tenant %s: %s", tid, _e)
            except Exception as e:
                log.warning(
                    "  HE key sweep failed for tenant %s: %s (continuing)",
                    tid,
                    type(e).__name__,
                )
        log.info("  Vault secrets for tenant %s purged", tid)
    except Exception as e:
        log.warning("  Vault purge failed for tenant %s: %s (continuing)", tid, e)

    # 4. Audit event — stamped BEFORE any DB-row change so the event ID
    # sequence is intact if a crash happens next.
    try:
        import crud
        crud.log_audit_event(
            db,
            actor_user_id=None,  # system-initiated
            action="TENANT_HARD_DELETED",
            resource_type="tenant",
            resource_id=str(tid),
            detail=(
                f"Tenant {tid} retention expired "
                f"(archived_at={tenant.deleted_at.isoformat() if tenant.deleted_at else 'unknown'}, "
                f"hard_delete_after={tenant.hard_delete_after.isoformat() if tenant.hard_delete_after else 'unknown'})."
            ),
            tenant_id=tid,
        )
    except Exception as e:
        log.warning("  Audit event TENANT_HARD_DELETED failed for tenant %s: %s", tid, e)

    # 5. Scrub the DB row. We don't actually delete the row — keeping a
    # stub preserves referential integrity on old audit entries that
    # FK onto tenant_id. The scrubbed row is functionally dead:
    # - name is already "Deleted Tenant N" from the archive step
    # - hard_delete_after cleared so the scheduler won't re-process it
    # - deleted_at stays set (row remains hidden from every listing)
    try:
        tenant.hard_delete_after = None
        tenant.encryption_key = None  # purge the tenant-local Fernet key
        db.add(tenant)
        db.commit()
        log.info("  DB row for tenant %s scrubbed (row kept for audit FK integrity)", tid)
    except Exception as e:
        log.error("  DB scrub failed for tenant %s: %s", tid, e)
        db.rollback()


# =========================================================================
# GSEC R-015/R-016 / FX.15 — privacy-erasure sweep (bytes + keys; rows stay)
# =========================================================================
#
# The retire / §11b sweeps above are gated off under iso_mode_on() (always
# on for SaaS), so soft-deleted tenant/user BYTES kept living on
# versitygw and in backups while the rows stayed (ISO 27001). This sweep
# is the resolution: ROWS stay, BYTES go. It destroys what the rows point
# at —
#   - object bytes in the tenant-{id}-files bucket (tenant + user files),
#   - the tenant's Fernet encryption key (crypto-purge: the Fernet-
#     encrypted tenant-secret copies in the DB + every backup that
#     carries them become unreadable),
#   - the raw tenant-secret dual-writes in Vault KV (they would otherwise
#     outlive the key),
# then stamps the row's purged timestamp (tenants.bytes_purged_at /
# users.erased_at) and records an AuditEvent (metadata only — no content).
#
# ISO-mode policy: this sweep RUNS in ISO mode (it deletes no rows, so the
# no-hard-delete rule holds). User-requested GDPR erasure of the user's
# own content bytes is honoured in both modes; platform records (rows,
# audit events, anonymized billing) are never destroyed. BYO-S3 tenants
# are report-only — we never touch customer buckets.

def _privacy_erasure_candidates(db, cutoff):
    """Tenants + users whose soft-delete anchor predates `cutoff` and
    whose erasure has not run yet. NULL anchors never qualify (SQL NULL
    comparison semantics).

    GY.V5 — a minor-labelled user (``users.is_minor``, the COPPA
    ``minor`` label) has an ACCELERATED erasure: the grace window
    (``privacy.erasure_grace_days``, read by the caller into ``cutoff``)
    is skipped — a minor qualifies as soon as ANY anchor is set,
    because their effective grace is 0 (see
    ``services/consent.erasure_grace_days_for``)."""
    from sqlalchemy import and_, or_
    tenants = (
        db.query(models.Tenant)
        .filter(models.Tenant.deleted_at.isnot(None))
        .filter(models.Tenant.deleted_at <= cutoff)
        .filter(models.Tenant.bytes_purged_at.is_(None))
        .all()
    )
    users = (
        db.query(models.User)
        .filter(models.User.erased_at.is_(None))
        .filter(
            or_(
                or_(
                    models.User.deleted_at <= cutoff,
                    models.User.deletion_requested_at <= cutoff,
                ),
                and_(
                    models.User.is_minor.is_(True),
                    or_(
                        models.User.deleted_at.isnot(None),
                        models.User.deletion_requested_at.isnot(None),
                    ),
                ),
            )
        )
        .all()
    )
    return tenants, users


def _purge_tenant_bucket_bytes(tenant_id: int, db) -> dict:
    """Empty + remove the house tenant-{id}-files bucket. Returns a
    {byo_report_only, bucket, objects, bytes} tally. Tenants with an
    ACTIVE BYO-S3 connection are report-only: the customer bucket is
    never touched (no client is even constructed for it)."""
    from crud import storage_connection_cud as sc_crud
    byo = sc_crud.get_active_connection(db, tenant_id=tenant_id)
    if byo is not None:
        return {
            "byo_report_only": True,
            "bucket": byo.bucket,
            "objects": 0,
            "bytes": 0,
        }
    from utils import storage as ustorage
    from utils.minio_client import get_tenant_bucket_name
    bucket = get_tenant_bucket_name(tenant_id, db)
    cfg = ustorage.get_storage_config()
    client = ustorage.build_client_for_config(cfg)
    removed = 0
    nbytes = 0
    try:
        if client.bucket_exists(bucket):
            for obj in client.list_objects(bucket, recursive=True):
                client.remove_object(bucket, obj.object_name)
                removed += 1
                nbytes += int(getattr(obj, "size", 0) or 0)
    except Exception as e:
        logger.warning(
            "privacy_erasure: tenant %s bucket %s empty failed: %s",
            tenant_id, bucket, e,
        )
    try:
        client.remove_bucket(bucket)
    except Exception as e:
        logger.warning(
            "privacy_erasure: tenant %s bucket %s remove failed: %s",
            tenant_id, bucket, e,
        )
    return {
        "byo_report_only": False,
        "bucket": bucket,
        "objects": removed,
        "bytes": nbytes,
    }


def _crypto_shred_tenant(db, tenant) -> dict:
    """Destroy the tenant's Fernet encryption key (crypto-purge) and wipe
    the Vault raw dual-writes it protects. Returns
    {key_destroyed, vault_secrets_wiped, vault_errors}. Vault failure is
    logged but not fatal — the DB-side key destruction is the primary
    purge action and still renders the DB copies unreadable."""
    key_destroyed = bool(tenant.encryption_key)
    tenant.encryption_key = None
    vault_wiped = 0
    vault_errors: list = []
    try:
        from utils.vault_client import approle_login, delete_kv_v2
        role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
        secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
        if role_id and secret_id:
            token = approle_login(role_id=role_id, secret_id=secret_id)
            for s in (
                db.query(models.Secret)
                .filter(models.Secret.tenant_id == tenant.id)
                .all()
            ):
                try:
                    delete_kv_v2(
                        f"tenants/{tenant.id}/secrets/{(s.name or '').strip()}",
                        token=token,
                    )
                    vault_wiped += 1
                except Exception as inner:
                    vault_errors.append(f"secrets/{s.name}: {inner}")
            try:
                delete_kv_v2(f"tenants/{tenant.id}/tenant_harbor", token=token)
                vault_wiped += 1
            except Exception as inner:
                vault_errors.append(f"tenant_harbor: {inner}")
            # FX-EXTFARM-ERASURE: farm credentials sit at
            # tenants/<tid>/external-farms/<fid>; wipe them with the same
            # helper/pattern as the other Vault wipes. Best-effort per farm —
            # one failure must not abort the rest of the erasure. Paths are
            # ints-only; no token or credential is ever logged.
            try:
                from services import external_farms as _efarm
                for farm in (
                    db.query(models.ExternalFarmConnection)
                    .filter(models.ExternalFarmConnection.tenant_id == tenant.id)
                    .all()
                ):
                    try:
                        delete_kv_v2(_efarm._farm_vault_path(tenant.id, farm.id),
                                     token=token)
                        vault_wiped += 1
                    except Exception as inner:
                        vault_errors.append(f"external-farms/{farm.id}: {inner}")
            except Exception as e:
                vault_errors.append(f"external-farms sweep: {e}")
            # ANS-Q15c-scim: plaintext SCIM bearer tokens (see the helper).
            try:
                _n, _errs = _erase_scim_tokens(
                    db, tenant.id, token, delete_kv_v2
                )
                vault_wiped += _n
                vault_errors.extend(_errs)
            except Exception as e:
                vault_errors.append(f"scim sweep: {type(e).__name__}")
            # ANS-Q15c-stacks: Vault-backed stack secret values (see the helper).
            try:
                _n, _errs = _erase_stack_secrets(
                    db, tenant.id, token, delete_kv_v2
                )
                vault_wiped += _n
                vault_errors.extend(_errs)
            except Exception as e:
                vault_errors.append(f"stack secrets sweep: {type(e).__name__}")
            # ANS-Q15c-he-keys: custodial HE key material (see the helper).
            try:
                _n, _errs = _erase_he_keys(
                    db, tenant.id, token, delete_kv_v2
                )
                vault_wiped += _n
                vault_errors.extend(_errs)
            except Exception as e:
                vault_errors.append(f"he-keys sweep: {type(e).__name__}")
    except Exception as e:
        vault_errors.append(f"vault: {e}")
    return {
        "key_destroyed": key_destroyed,
        "vault_secrets_wiped": vault_wiped,
        "vault_errors": vault_errors,
    }


def _erase_tenant_bytes(db, tenant, record_ledger: bool = True) -> None:
    """FX.15 R-015 — destroy one soft-deleted tenant's object bytes +
    encryption key, stamp bytes_purged_at, record a TENANT_BYTES_PURGED
    audit event. The row itself stays (ISO 27001). Idempotent via
    bytes_purged_at; refuses the platform tenant (id=1).

    ``record_ledger`` (default True) also appends the erasure to the
    append-only restore-safety ledger. Replay passes False so re-applying an
    already-logged erasure does not duplicate the ledger entry."""
    if tenant.id == 1:
        logger.warning("privacy_erasure: refusing platform tenant id=1")
        return
    if tenant.deleted_at is None or tenant.bytes_purged_at is not None:
        return
    tally = _purge_tenant_bucket_bytes(tenant.id, db)
    shred = _crypto_shred_tenant(db, tenant)
    tenant.bytes_purged_at = datetime.now(timezone.utc)
    db.add(models.AuditEvent(
        tenant_id=tenant.id,
        event_name="TENANT_BYTES_PURGED",
        details={
            "bucket": tally["bucket"],
            "byo_report_only": tally["byo_report_only"],
            "objects_removed": tally["objects"],
            "bytes_removed": tally["bytes"],
            "encryption_key_destroyed": purge["key_destroyed"],
            "vault_secrets_wiped": purge["vault_secrets_wiped"],
            "vault_errors": purge["vault_errors"],
            "anchor_deleted_at": tenant.deleted_at.isoformat(),
        },
    ))
    if record_ledger:
        # Q3(c) — FX.15 restore-safety: record the erasure in the append-only
        # ledger (ID-only, no PII) so a later restore can replay it.
        # 1788l CE carve: services/erasure_ledger.py is denied in CE (open-core/
        # manifest.yaml), so CE keeps NO restore-safety ledger; the erasure itself
        # (the ISO 27001 requirement) still runs unchanged — the ledger is a paid
        # replay side effect only.
        try:
            from services import erasure_ledger as _erledger
        except ImportError:
            _erledger = None  # not in this edition — skip the ledger write
        if _erledger is not None:
            try:
                _erledger.append_erasure(
                    db, subject_type="tenant", subject_id=tenant.id,
                    erased_at=tenant.bytes_purged_at, tenant_id=tenant.id,
                    scope="tenant_bytes_and_key",
                )
            except Exception as e:  # noqa: BLE001 — a ledger failure must not abort erasure
                logger.error("privacy_erasure: tenant %s ledger write failed: %s", tenant.id, e)
    db.commit()
    logger.info(
        "privacy_erasure: tenant %s bytes purged (objects=%d bytes=%d byo_report_only=%s key_destroyed=%s)",
        tenant.id, tally["objects"], tally["bytes"],
        tally["byo_report_only"], purge["key_destroyed"],
    )


def _tenant_was_reactivated(tenant) -> bool:
    """True when the tenant's FX.92 cancellation window was REACTIVATED.

    ``subscription.cancellation_window`` is a JSONB dict (read as a dict
    only — NEVER ``.astext``): the resubscribe path
    (services/cancellation_lifecycle.reactivate) stamps
    ``status = 'reactivated'`` + ``reactivated_at``. A tenant in that
    state is back in normal operation, so the day-60 crypto-purge must
    never run for it even if a stale ``deleted_at`` stamp survives on
    the row. Any lookup failure is treated as "reactivated" (fail closed
    — the purge is an irreversible destruction, so an uncertain state
    must never trigger it)."""
    try:
        sub = getattr(tenant, "subscription", None)
        cw = sub.cancellation_window if sub is not None else None
    except Exception:  # noqa: BLE001 — fail closed (see docstring)
        return True
    return isinstance(cw, dict) and cw.get("status") == "reactivated"


def _run_tenant_key_shred(db=None):
    """FX.59 p5a — daily 04:35 UTC tenant-key crypto-purge sweep.

    Tenants whose deletion was requested at least
    ``tenant.deletion_purge_after_days`` (platform setting, default 60)
    ago — i.e. ``tenants.deleted_at`` (the soft-delete stamp the delete
    endpoints / cancellation-window finalization record) at or before
    the cutoff — have their tenant encryption key destroyed: the
    per-tenant Vault transit key (when the Vault-transit KEK backend is
    vault_transit or the stored key is vault-wrapped) plus the DB-side
    copy. A tenant that was REACTIVATED (its subscription's
    ``cancellation_window.status`` == 'reactivated' — FX.92 resubscribe
    during the cancellation window) is NEVER touched, even if a stale
    ``deleted_at`` stamp survives on the row: the customer is back in
    normal operation and their data must stay readable. ONE append-only
    deletion attestation
    (services.deletion_attestation.record_attestation) is written per
    tenant with the key identifiers (never key material) and evidence
    counts. Rows are NEVER deleted (ISO 27001) — the tenant row keeps
    its ``deleted_at`` stamp; the purge is recorded by the audit row +
    the attestation. Idempotent: a tenant that already has an
    attestation is skipped. A legal hold blocks the purge (fail closed,
    matching retire_archived_tenants / _erase_tenant_bytes); a failed
    purge writes nothing and retries on the next tick.

    While ``tenant.crypto_purge_enabled`` is False (default) the sweep
    runs in DRY mode: it logs which tenants are due and reports them —
    nothing is destroyed and NO attestation is written.

    ``db`` is injectable for tests; the scheduler path opens its own
    session via ``_get_db()``. Both settings are re-read on every tick.
    """
    own_db = db is None
    if own_db:
        db = _get_db()
    try:
        enabled = bool(_psr.get_setting("tenant.crypto_shred_enabled", db=db))
        days = int(_psr.get_setting("tenant.deletion_shred_after_days",
                                    db=db) or 60)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        attested = set(
            row[0]
            for row in db.query(models.TenantDeletionAttestation.tenant_id).all()
        )
        candidates = (
            db.query(models.Tenant)
            .filter(models.Tenant.deleted_at.isnot(None))
            .filter(models.Tenant.deleted_at <= cutoff)
            .all()
        )
        due = [
            t for t in candidates
            if t.id != 1
            and t.id not in attested
            # FX.59 p5a — a REACTIVATED tenant is never crypto-purged
            # (owner Q42=A: "reactivating within 30 more days restores
            # everything"). The FX.92 cancellation-window state is the
            # authority: a resubscribe stamps cancellation_window.status
            # = 'reactivated', and a stale/late deleted_at stamp must
            # never resurrect the purge for a customer who is back in
            # normal operation.
            and not _tenant_was_reactivated(t)
        ]
        if not due:
            logger.debug("tenant_key_purge: nothing due (cutoff=%s)", cutoff)
            return
        if not enabled:
            # DRY mode — report only: no destruction, no attestation.
            logger.info(
                "tenant_key_purge: DRY run (tenant.crypto_purge_enabled=false) "
                "— %d tenant(s) due but nothing purged: ids=%s",
                len(due), [t.id for t in due],
            )
            return
        from utils.paid_import import optional_paid
        _lhold = optional_paid("services.legal_hold")
        shredded = 0
        for tenant in due:
            try:
                if _lhold is not None and _lhold.is_held(db,
                                                         tenant_id=tenant.id):
                    logger.info(
                        "tenant_key_purge: skipping tenant %s — legal hold "
                        "active; key preserved", tenant.id,
                    )
                    continue
            except Exception as e:  # noqa: BLE001 — fail closed
                logger.error(
                    "tenant_key_purge: hold lookup failed for tenant %s — "
                    "failing closed (skipping purge): %s", tenant.id, e,
                )
                continue
            try:
                _shred_one_tenant_key(db, tenant)
                shredded += 1
            except Exception as e:  # noqa: BLE001 — keep going per tenant
                logger.error("tenant_key_purge: tenant %s purge failed: %s",
                             tenant.id, e)
        logger.info("tenant_key_purge: purged %d/%d due tenant(s)",
                    shredded, len(due))
    finally:
        if own_db:
            db.close()


def _shred_one_tenant_key(db, tenant) -> None:
    """FX.59 p5a — destroy ONE tenant's encryption key, stamp the audit
    row, write its append-only deletion attestation.

    Success = every key that was supposed to die is destroyed (the
    per-tenant Vault transit key when applicable + the DB-side copy).
    On any failure nothing is written and the tenant stays un-attested
    so the next tick retries — an attestation is a fact, and a failed
    purge is not one. The tenant row is never deleted (ISO 27001): its
    ``deleted_at`` soft-delete stamp is the deletion-request record and
    stays as-is."""
    # 1788l CE carve: services/deletion_attestation.py + services/key_provider.py are
    # both denied in CE (open-core/manifest.yaml), so the whole FX.59 p5a crypto-purge
    # (Vault transit destroy + the append-only attestation) is the paid feature: CE
    # no-ops it — the job is also skipped at registration time, so this guard only
    # fires for direct calls. The DB-side key nulling is NOT done in CE: without the
    # attestation the sweep has no idempotency record, so a partial CE purge would
    # destroy keys + re-log every tenant on every tick forever.
    try:
        from services import deletion_attestation, key_provider
    except ImportError:
        logger.debug("tenant_key_purge: not in this edition (paid purge modules absent)")
        return
    key_ids = []
    errors = []
    transit_destroyed = False
    transit_expected = (
        key_provider.current_provider(db=db) == "vault_transit"
        or key_provider.is_vault_wrapped(tenant.encryption_key)
    )
    if transit_expected:
        result = key_provider.destroy_tenant_transit_key(tenant.id, db=db)
        key_ids.append(result["key_name"])
        if result["destroyed"]:
            transit_destroyed = True
        else:
            errors.append(result["error"] or "transit key destroy failed")
    db_key_destroyed = bool(tenant.encryption_key)
    if db_key_destroyed:
        # DB-side purge (rows stay — same semantics as FX.15's
        # _crypto_purge_tenant).
        tenant.encryption_key = None
    if errors:
        logger.error(
            "tenant_key_purge: tenant %s purge INCOMPLETE (will retry "
            "next tick): %s", tenant.id, errors,
        )
        return
    db.add(models.AuditEvent(
        tenant_id=tenant.id,
        event_name="TENANT_KEY_CRYPTO_PURGE",
        details={
            "method": "tenant_key_crypto_purge",
            "transit_key_destroyed": transit_destroyed,
            "db_key_destroyed": db_key_destroyed,
            "anchor_deleted_at": (
                tenant.deleted_at.isoformat() if tenant.deleted_at else None),
        },
    ))
    db.commit()
    deletion_attestation.record_attestation(
        db,
        tenant_id=tenant.id,
        tenant_name=tenant.name or f"tenant-{tenant.id}",
        requested_at=tenant.deleted_at,
        shredded_at=datetime.now(timezone.utc),
        method="tenant_key_crypto_purge",
        key_ids=key_ids,
        evidence={
            "transit_key_destroyed": transit_destroyed,
            "db_key_destroyed": db_key_destroyed,
        },
    )
    logger.info(
        "tenant_key_purge: tenant %s purged (transit=%s db_key=%s)",
        tenant.id, transit_destroyed, db_key_destroyed,
    )


def _privacy_scheme() -> str:
    """Storage URL scheme (deployment config, not behaviour). `s3` is the
    primary default; `minio` stays a REAL accepted value for existing
    deployments (STORAGE_SCHEME=minio)."""
    return os.getenv("STORAGE_SCHEME", "s3")


def _storage_schemes() -> tuple:
    """Schemes to accept when parsing a stored storage_path (dual-read)."""
    configured = _privacy_scheme()
    return tuple(dict.fromkeys((configured, "s3", "minio")))


def _privacy_object_key(storage_path: str, bucket: str) -> str:
    """Object key for a `s3://bucket/key` (or legacy `minio://bucket/key`)
    storage_path. The stored row is never rewritten — the prefix is stripped
    only here, at the point of use. Bare keys pass through unchanged; an
    unknown scheme is returned unchanged so the sweep fails closed instead of
    removing a guessed name."""
    if not storage_path:
        return ""
    for scheme in _storage_schemes():
        if storage_path.startswith(f"{scheme}://{bucket}/"):
            return storage_path[len(f"{scheme}://{bucket}/"):]
        if storage_path.startswith(f"{scheme}://"):
            after = storage_path[len(f"{scheme}://"):]
            return "/".join(after.split("/")[1:]) if "/" in after else ""
    return storage_path


def _erase_user_bytes(db, user, record_ledger: bool = True) -> None:
    """FX.15 R-016 — erase one deletion-requested / soft-deleted user:
    apply the full PII scrub + soft-delete (idempotent via
    crud.delete_user_permanently), remove the objects their live File
    rows reference from the tenant bucket (report-only for BYO-S3),
    stamp erased_at, record a USER_BYTES_PURGED audit event. The row
    itself stays (ISO 27001). Idempotent via erased_at.

    ``record_ledger`` (default True) also appends the erasure to the
    append-only restore-safety ledger. Replay passes False so re-applying an
    already-logged erasure does not duplicate the ledger entry."""
    if user.erased_at is not None:
        return
    tid = user.tenant_id
    uid = user.id
    if user.deleted_at is None:
        # Account-deletion request path: the grace window has passed, so
        # apply the same scrub the tenant-delete cascade uses (PII
        # scrub + deleted_at stamp + API-key revoke).
        import crud
        crud.delete_user_permanently(db=db, db_user=user)
    byo = False
    removed = 0
    nbytes = 0
    bucket = None
    try:
        from crud import storage_connection_cud as sc_crud
        byo = sc_crud.get_active_connection(db, tenant_id=tid) is not None
        paths = {
            p
            for (p,) in db.query(models.File.storage_path)
            .filter(
                models.File.user_id == uid,
                models.File.deleted_at.is_(None),
                models.File.storage_path.isnot(None),
            )
            .all()
        }
        if paths and not byo and tid is not None:
            from utils import storage as ustorage
            from utils.minio_client import get_tenant_bucket_name
            bucket = get_tenant_bucket_name(tid, db)
            cfg = ustorage.get_storage_config()
            client = ustorage.build_client_for_config(cfg)
            for p in paths:
                key = _privacy_object_key(p, bucket)
                try:
                    st = client.stat_object(bucket, key)
                    nbytes += int(getattr(st, "size", 0) or 0)
                    client.remove_object(bucket, key)
                    removed += 1
                except Exception as e:
                    logger.warning(
                        "privacy_erasure: user %s object remove failed (%s): %s",
                        uid, p, e,
                    )
    except Exception as e:
        logger.warning("privacy_erasure: user %s file purge failed: %s", uid, e)
    user.erased_at = datetime.now(timezone.utc)
    db.add(models.AuditEvent(
        tenant_id=tid,
        user_id=uid,
        event_name="USER_BYTES_PURGED",
        details={
            "bucket": bucket,
            "byo_report_only": byo,
            "objects_removed": removed,
            "bytes_removed": nbytes,
        },
    ))
    if record_ledger:
        # Q3(c) — FX.15 restore-safety: record the erasure in the append-only
        # ledger (ID-only, no PII) so a later restore can replay it.
        # 1788l CE carve: services/erasure_ledger.py is denied in CE, so CE keeps NO
        # restore-safety ledger; the user erasure itself still runs unchanged.
        try:
            from services import erasure_ledger as _erledger
        except ImportError:
            _erledger = None  # not in this edition — skip the ledger write
        if _erledger is not None:
            try:
                _erledger.append_erasure(
                    db, subject_type="user", subject_id=uid,
                    erased_at=user.erased_at, tenant_id=tid,
                    scope="user_pii_and_bytes",
                )
            except Exception as e:  # noqa: BLE001 — a ledger failure must not abort erasure
                logger.error("privacy_erasure: user %s ledger write failed: %s", uid, e)
    db.commit()
    logger.info(
        "privacy_erasure: user %s (tenant %s) erased (objects=%d bytes=%d byo_report_only=%s)",
        uid, tid, removed, nbytes, byo,
    )


def _run_biometric_destruction():
    """GY.V8 (BIPA 740 ILCS 14) — daily 05:40 UTC scheduled-destruction sweep.

    Soft-deletes biometric-labelled outputs past their subject's consent
    retention deadline (the stated retention period, or 3 years from the
    subject's last interaction — whichever is FIRST) through the existing
    soft-delete erasure path (``services/consent.queue_biometric_destruction``),
    skipping every row under an active legal hold. Rows are NEVER
    hard-deleted (ISO 27001); the bytes then follow the normal soft-delete
    byte-purge lifecycle. Off unless
    ``privacy.biometric_destruction_job_enabled`` is True."""
    if not _psr.get_setting("privacy.biometric_destruction_job_enabled",
                            db=None):
        logger.info(
            "biometric_destruction: disabled "
            "(privacy.biometric_destruction_job_enabled=False)")
        return
    db = _get_db()
    try:
        from services import consent as _consent
        out = _consent.queue_biometric_destruction(db, dry_run=False)
        logger.info("biometric_destruction: applied — %s", out)
    except Exception as e:  # noqa: BLE001 — a sweep failure must not raise
        logger.error("biometric_destruction failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _run_privacy_erasure():
    """FX.15 (GSEC R-015/R-016) — daily 05:30 UTC privacy-erasure sweep.

    For every user/tenant soft-deleted at least
    ``privacy.erasure_grace_days`` ago (platform setting, default 30):
    destroy the object-store bytes, crypto-purge the tenant key, stamp
    the row's purged timestamp, and record the purge in audit_events.
    No rows are ever deleted — ISO 27001 holds even in ISO mode, where
    this sweep is the only purge path (the retire / §11b sweeps are
    gated off). BYO-S3 tenants are report-only: customer buckets are
    never touched.

    Off unless ``privacy.erasure_job_enabled`` is True: byte destruction
    is irreversible, so an install opts in explicitly (owner decision)."""
    if not _psr.get_setting("privacy.erasure_job_enabled", db=None):
        logger.info("privacy_erasure: disabled (privacy.erasure_job_enabled=False)")
        return
    grace = int(_psr.get_setting("privacy.erasure_grace_days", db=None) or 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace)
    db = _get_db()
    try:
        from utils.paid_import import optional_paid
        _lhold = optional_paid("services.legal_hold")
        tenants, users = _privacy_erasure_candidates(db, cutoff)
        for t in tenants:
            # GY.C15 — legal hold / preserve mode: a held tenant's bytes
            # must NOT be destroyed (is_held fails closed: a lookup error
            # counts as held). Supports preservation requirements (e.g.
            # DFARS 252.204-7012 90-day).
            try:
                if _lhold is not None and _lhold.is_held(db,
                                                         tenant_id=t.id):
                    logger.info(
                        "privacy_erasure: skipping tenant %s — legal hold "
                        "active; bytes preserved", t.id,
                    )
                    continue
            except Exception as e:  # noqa: BLE001 — fail closed
                logger.error(
                    "privacy_erasure: hold lookup failed for tenant %s — "
                    "failing closed (skipping erasure): %s", t.id, e,
                )
                continue
            try:
                _erase_tenant_bytes(db, t)
            except Exception as e:
                logger.error("privacy_erasure: tenant %s failed: %s", t.id, e)
                try:
                    db.rollback()
                except Exception:
                    pass
        for u in users:
            # GY.C15 — legal hold: skip a user when any hold covers the
            # user's tenant or the user themself (user-scoped hold).
            # is_held fails closed (a lookup error counts as held).
            try:
                if _lhold is not None and _lhold.is_held(
                        db, tenant_id=u.tenant_id, user_id=u.id):
                    logger.info(
                        "privacy_erasure: skipping user %s (tenant %s) — "
                        "legal hold active; bytes preserved", u.id, u.tenant_id,
                    )
                    continue
            except Exception as e:  # noqa: BLE001 — fail closed
                logger.error(
                    "privacy_erasure: hold lookup failed for user %s — "
                    "failing closed (skipping erasure): %s", u.id, e,
                )
                continue
            # Q3b — GDPR erasure prerequisite (b): scrub this user's PII
            # out of the append-only audit rows (details/audit_metadata
            # JSONB) BEFORE _erase_user_bytes scrubs the user row, so the
            # original PII is still available to match. A scrub failure
            # must not abort the sweep (the row stays; a later replay /
            # re-run can scrub it). Held users were skipped above.
            # GY.V7 — an audit-scoped hold freezes this tenant's audit
            # records: the PII scrub (an audit-records GC) is skipped;
            # is_held(audit=True) fails closed (a lookup error counts as
            # held, so the audit records stay). Byte-erasure still
            # proceeds — the hold covers the audit trail, not the
            # user's object-store bytes (the Q3(b) erasure path).
            try:
                if _lhold is not None and _lhold.is_held(
                        db, tenant_id=u.tenant_id, audit=True):
                    logger.info(
                        "privacy_erasure: skipping audit PII scrub for "
                        "user %s (tenant %s) — audit-scoped legal hold "
                        "active; audit records preserved",
                        u.id, u.tenant_id,
                    )
                elif _module_available(
                        "services.privacy.audit_pii_scrub"):
                    from services.privacy import audit_pii_scrub as _api_scrub
                    _api_scrub.scrub_audit_pii_for_user(db, u.id, email=u.email)
                # CE build (open-core/manifest.yaml deny): the audit PII
                # scrub module is stripped from the tree, so this step is
                # skipped silently — the audit trail is not part of CE, and
                # the CE erasure feature (anonymising the user, destroying
                # bytes + keys below) still runs. No ERROR/WARNING: a
                # missing paid module is not a failure.
            except Exception as e:  # noqa: BLE001 — fail closed (audit stays)
                logger.error(
                    "privacy_erasure: user %s audit PII scrub failed — "
                    "audit records preserved (fail closed): %s", u.id, e,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
            try:
                _erase_user_bytes(db, u)
            except Exception as e:
                logger.error("privacy_erasure: user %s failed: %s", u.id, e)
                try:
                    db.rollback()
                except Exception:
                    pass

        # GSEC R-088 / FX.32 — house-side BYO duplicates. A verified BYO
        # migration leaves a full duplicate in the HOUSE bucket; the copy
        # itself is verified per-object in services/storage_migration.py and
        # scheduled with storage.byo_house_copy_grace_days (default 30). This
        # is the same sweep, so the grace-due house copies are purged here
        # through the existing erasure mechanism (no new deleter).
        try:
            from services import storage_migration as _stmig
            house = _stmig.run_house_copy_erasure(db)
            if house.get("tenants"):
                logger.info(
                    "privacy_erasure: BYO house copies purged — tenants=%d objects=%d bytes=%d",
                    house.get("tenants", 0), house.get("objects", 0), house.get("bytes", 0),
                )
        except Exception as e:  # noqa: BLE001 — a sweep failure must not raise
            logger.error("privacy_erasure: BYO house-copy sweep failed: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        db.close()


def _run_scheduler_pseudonymise():
    """FX.40 part 1 (owner decision Q22=A) — daily scheduler-data
    pseudonymisation sweep.

    Stamps api_task_queue rows older than ``scheduler.pseudonymise_after_days``
    (default 90) with ``tenant_pseudonym`` = HMAC-SHA256(Vault key, tenant_id)
    truncated to 32 hex chars ("p_" prefix) plus ``pseudonymised_at``, so the
    scheduler retrain corpus (api_task_queue joined with task_resource_samples
    / prediction_logs) can be keyed by pseudonym instead of tenant_id.

    Off unless ``scheduler.pseudonymise_job_enabled`` is True. With no key at
    Vault path ``infrastructure/scheduler/pseudonym_key`` the sweep logs a
    WARNING and changes nothing (it never generates or prints a key). No rows
    are ever deleted and ``tenant_id`` is never nulled here — the column is
    nullable=False, so nulling it is part 2 (ISO 27001: no hard deletes).
    """
    _fx40 = optional_paid("services.scheduler_pseudonymise")
    if _fx40 is None:
        return
    db = _get_db()
    try:
        _fx40.run_pseudonymise_sweep(db)
    except Exception as e:  # noqa: BLE001 — a sweep failure must not kill the loop
        logger.error("scheduler_pseudonymise: sweep failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _run_scheduler_aggregate():
    """FX.40 part 2 (owner decision Q22=A) — daily scheduler-data
    aggregate roll-up sweep.

    Folds api_task_queue rows older than ``scheduler.aggregate_after_months``
    (default 24) into the tenant-free ``scheduler_training_aggregates`` table
    (node_kind / queue / gpu_model / month / success buckets with count +
    duration p50/p95 + peak-resource stats — exactly the columns the
    retrain reads) and soft-marks the source rows ``aggregated_at``.

    Off unless ``scheduler.aggregate_job_enabled`` is True. No rows are ever
    deleted and ``tenant_id`` is never nulled (ISO 27001: no hard deletes; the
    column is nullable=False so it is out of scope for Q22 part 2).
    """
    _fx40 = optional_paid("services.scheduler_pseudonymise")
    if _fx40 is None:
        return
    db = _get_db()
    try:
        _fx40.run_aggregate_sweep(db)
    except Exception as e:  # noqa: BLE001 — a sweep failure must not kill the loop
        logger.error("scheduler_aggregate: sweep failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _run_check_harbor_quotas():
    """Hourly sweep. For every active tenant with a non-zero Harbor
    quota: email + audit once per 24h at 80%, flip quota_push_blocked
    at 95%. Clears quota_push_blocked when usage drops back below 95%
    (self-healing after the tenant deletes old images)."""
    import os
    from datetime import timedelta
    # GS.PSR.8 — harbor.tenant_harbor_enabled (DB > env > True).
    _carve_m = optional_paid("utils.tenant_harbor_client")
    if _carve_m is None:
        return
    tenant_harbor_enabled = getattr(_carve_m, "tenant_harbor_enabled")
    if not tenant_harbor_enabled():
        return

    db = _get_db()
    try:
        import models
        _carve_m = optional_paid("utils.tenant_harbor_client")
        if _carve_m is None:
            return
        TenantHarborClient = getattr(_carve_m, "TenantHarborClient")
        harbor = TenantHarborClient()
        now = datetime.now(timezone.utc)
        dedup_window = now - timedelta(hours=24)

        active_tenants = (
            db.query(models.Tenant)
            .filter(models.Tenant.deleted_at.is_(None))
            .all()
        )
        for tenant in active_tenants:
            if tenant.id == 1:
                continue  # platform tenant has no Harbor project
            try:
                q = harbor.get_project_quota(tenant.id)
            except Exception as e:
                logger.debug("quota fetch failed for tenant %s: %s", tenant.id, e)
                continue
            used = q.get("used_bytes", 0)
            hard = q.get("hard_quota_bytes", -1)
            if hard is None or hard <= 0:
                continue  # unlimited or missing — nothing to alert on

            ratio = used / hard
            new_blocked = ratio >= 0.95
            if new_blocked != tenant.quota_push_blocked:
                tenant.quota_push_blocked = new_blocked
                db.add(tenant)
                db.commit()
                logger.info(
                    "tenant %s quota_push_blocked=%s (used=%s / hard=%s)",
                    tenant.id, new_blocked, used, hard,
                )

            # Threshold dispatch: only fire once per 24h per threshold.
            for threshold in (95, 80):
                if ratio * 100 < threshold:
                    continue
                already = (
                    db.query(models.HarborQuotaAlert)
                    .filter(models.HarborQuotaAlert.tenant_id == tenant.id)
                    .filter(models.HarborQuotaAlert.threshold == threshold)
                    .filter(models.HarborQuotaAlert.sent_at >= dedup_window)
                    .first()
                )
                if already:
                    continue
                _dispatch_quota_alert(db, tenant, threshold, used, hard)
                # Only emit the HIGHEST tripped threshold per pass to
                # avoid double-emailing (80% alert + 95% alert for the
                # same jump).
                break
    finally:
        db.close()


def _dispatch_quota_alert(db, tenant, threshold: int, used: int, hard: int) -> None:
    """Record + (try to) email + audit one quota-threshold crossing."""
    import models
    try:
        row = models.HarborQuotaAlert(
            tenant_id=tenant.id,
            threshold=threshold,
            email_sent=False,
        )
        db.add(row)
        db.commit()
    except Exception as e:
        logger.warning("Could not record usage alert: %s", e)
        db.rollback()
        return

    # Audit event — always logged, even if the email dispatch fails.
    try:
        import crud
        crud.log_audit_event(
            db,
            actor_user_id=None,
            action=(
                "TENANT_QUOTA_WARNING_80"
                if threshold == 80 else
                "TENANT_QUOTA_BLOCKED_95"
            ),
            resource_type="tenant",
            resource_id=str(tenant.id),
            detail=(
                f"Tenant {tenant.id} Harbor storage at {threshold}% "
                f"(used={used}, hard={hard})."
            ),
            tenant_id=tenant.id,
        )
    except Exception as e:
        logger.warning("Audit event for usage alert failed: %s", e)

    # Best-effort email. billing_email falls back to the tenant's
    # parent user email; if neither is set we still logged the audit.
    try:
        recipient = getattr(tenant, "billing_email", None)
        if not recipient:
            parent = (
                db.query(models.User)
                .filter(models.User.tenant_id == tenant.id)
                .filter(models.User.parent_user_id.is_(None))
                .filter(models.User.deleted_at.is_(None))
                .first()
            )
            recipient = getattr(parent, "email", None) if parent else None
        if recipient:
            subject = (
                "Action required: your registry storage is almost full"
                if threshold == 80 else
                "Push blocked: your registry storage is full"
            )
            body = (
                f"Your tenant's container registry has reached {threshold}% "
                f"of its storage quota.\n\n"
                f"Used: {used:,} bytes\nLimit: {hard:,} bytes\n\n"
                + ("New pushes are now BLOCKED until you free space.\n"
                   if threshold == 95 else
                   "New pushes will be blocked at 95%. Visit the dashboard to delete "
                   "unused images.\n")
            )
            from utils.email_sender import send_templated_email
            send_templated_email(
                db=db,
                tenant_id=tenant.id,
                template_name="generic-notice",
                subject=subject,
                body_plain=body,
                recipient_email=recipient,
            )
            row.email_sent = True
            db.add(row)
            db.commit()
    except Exception as e:
        logger.warning("Usage alert email dispatch failed for tenant %s: %s", tenant.id, e)


def _run_snapshot_harbor_usage():
    """Daily: record every active tenant's Harbor used_bytes +
    artifact_count into tenant_harbor_usage_daily. Monthly billing
    reads the peak value from these rows to bill storage overage."""
    import os
    # GS.PSR.8 — harbor.tenant_harbor_enabled (DB > env > True).
    _carve_m = optional_paid("utils.tenant_harbor_client")
    if _carve_m is None:
        return
    tenant_harbor_enabled = getattr(_carve_m, "tenant_harbor_enabled")
    if not tenant_harbor_enabled():
        return

    db = _get_db()
    try:
        import models
        _carve_m = optional_paid("utils.tenant_harbor_client")
        if _carve_m is None:
            return
        TenantHarborClient = getattr(_carve_m, "TenantHarborClient")
        harbor = TenantHarborClient()

        active_tenants = (
            db.query(models.Tenant)
            .filter(models.Tenant.deleted_at.is_(None))
            .all()
        )
        count = 0
        for tenant in active_tenants:
            # The platform tenant (id 1) was skipped, leaving the platform
            # admin with no storage visibility for their own node images.
            # Meter it too — it's on the unlimited plan so this is
            # measurement/visibility, not overage billing.
            try:
                q = harbor.get_project_quota(tenant.id)
                summary = harbor.get_project_summary(tenant.id)
            except Exception as e:
                logger.debug("usage snapshot fetch failed for tenant %s: %s", tenant.id, e)
                continue
            artifact_count = 0
            if isinstance(summary, dict):
                artifact_count = int(summary.get("repo_count") or 0)
            try:
                row = models.TenantHarborUsageDaily(
                    tenant_id=tenant.id,
                    used_bytes=q.get("used_bytes", 0) or 0,
                    artifact_count=artifact_count,
                )
                db.add(row)
                db.commit()
                count += 1
            except Exception as e:
                # Likely the uniqueness constraint — snapshot already
                # ran today. Roll back and move on.
                db.rollback()
                logger.debug("usage snapshot already recorded for tenant %s today: %s", tenant.id, e)
        logger.info("snapshot_harbor_usage: recorded %d tenant row(s)", count)
    finally:
        db.close()


def _fleet_devices(db, active_workers) -> dict:
    """Per-worker accelerator inventory: {worker_name: {kind: [vram desc]}}.

    "" holds every device regardless of kind, mirroring the claim-time guard in
    worker_tasks._try_claim_task so the sweep's verdict and the claim's verdict
    cannot disagree.
    """
    import models
    out: dict = {}
    ids = {w.hardware_profile_id: w.worker_name for w in active_workers if w.hardware_profile_id}
    if not ids:
        return out
    rows = (db.query(models.AcceleratorProfile.hardware_profile_id,
                     models.AcceleratorProfile.kind,
                     models.AcceleratorProfile.vram_gb)
            .filter(models.AcceleratorProfile.hardware_profile_id.in_(list(ids.keys())),
                    models.AcceleratorProfile.vram_gb.isnot(None)).all())
    for hp_id, kind, vram in rows:
        name = ids.get(hp_id)
        if not name:
            continue
        d = out.setdefault(name, {})
        d.setdefault("", []).append(float(vram))
        d.setdefault(str(kind).strip().lower(), []).append(float(vram))
    for d in out.values():
        for k in d:
            d[k].sort(reverse=True)
    return out


def _accel_shortfall(demand: dict, fleet: dict):
    """Return None if some worker satisfies `demand`, else a legible reason.

    "No eligible worker" is useless to an operator. This names the shortfall and
    the best capacity actually observed, so the answer to "why is my stack stuck"
    is "your biggest cuda card is 24 GB and it needs 48".
    """
    if not demand:
        return None
    best_seen: dict = {}
    for name, devices in fleet.items():
        ok = True
        for kind, reqs in demand.items():
            have = devices.get(kind or "", [])
            if have:
                cur = best_seen.get(kind or "", 0.0)
                best_seen[kind or ""] = max(cur, have[0])
            for i, req in enumerate(sorted(reqs, reverse=True)):
                if i >= len(have) or have[i] < float(req):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            return None
    parts = []
    for kind, reqs in demand.items():
        want = ", ".join(f"{float(r):g} GB" for r in sorted(reqs, reverse=True))
        label = kind or "any"
        seen = best_seen.get(kind or "")
        observed = (f"largest {label} device seen: {seen:g} GB" if seen
                    else f"no active worker reports a {label} accelerator")
        parts.append(f"needs {len(reqs)} {label} device(s) [{want}] — {observed}")
    return ("No active worker satisfies the accelerator demand: "
            + "; ".join(parts) + ".")


def _capability_shortfall(required, active_tags):
    """Return None if every required capability is present somewhere in the
    fleet, else a reason naming the missing ones. AND semantics, matching the
    claim guard — a precondition list means every entry must hold."""
    if not isinstance(required, list) or not required:
        return None
    want = {str(c).strip().lower() for c in required if isinstance(c, str)}
    missing = sorted(want - active_tags)
    if not missing:
        return None
    return (f"No active worker reports required capability "
            f"{{{', '.join(missing)}}}.")


# How long a stack may sit in a transitional status with no task in flight
# before its accelerators are reclaimed. Must comfortably exceed the ordinary
# dispatch-to-claim window (worker long-poll is 60s) so a slow claim is never
# mistaken for a dead operation.
STACK_ORPHAN_GRACE_MINUTES = 15


def _run_stranded_stack_task_sweep():
    """Fail stack tasks pinned to a worker that is never coming back.

    Targeted stack ops — teardown, snapshot, restore, migrate — are pinned to
    the host holding the containers, which is correct: only that box can act on
    them. But nothing reclaims a pinned task when that worker is decommissioned
    or has its license revoked. The task sits ASSIGNED forever, the completion
    handler never runs, and the stack row keeps whatever status the dispatcher
    optimistically set.

    Found on the dev deployment: a stack was re-placed onto a second worker,
    that worker was then removed, and its teardown sat ASSIGNED indefinitely
    while the stack read `torn_down`. The dangerous shape of that is a teardown
    the operator believes completed, against containers still running on a box
    nobody is talking to.

    OFFLINE is deliberately NOT swept — a worker that dropped out may come back
    and finish the job, and reclaiming its work early would duplicate the op.
    Only a worker that is UNLICENSED or gone from the table is treated as never
    returning.
    """
    import models
    from datetime import timedelta

    db = _get_db()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STACK_ORPHAN_GRACE_MINUTES)
        pending = (db.query(models.ApiTaskQueue)
                   .filter(models.ApiTaskQueue.status.in_([
                               models.ApiTaskStatus.PENDING,
                               models.ApiTaskStatus.ASSIGNED]),
                           models.ApiTaskQueue.assigned_to_worker.isnot(None),
                           models.ApiTaskQueue.created_at <= cutoff)
                   .limit(200).all())

        stranded = 0
        for task in pending:
            payload = task.task_payload or {}
            stack_op = payload.get("stack_op")
            if not stack_op:
                continue

            worker = (db.query(models.WorkerLicense)
                      .filter(models.WorkerLicense.worker_name
                              == task.assigned_to_worker,
                              models.WorkerLicense.deleted_at.is_(None)).first())
            if worker is not None and worker.status != models.WorkerStatus.UNLICENSED:
                continue      # gone quiet, not gone for good

            task.status = models.ApiTaskStatus.FAILED
            task.error_message = (
                f"stranded: pinned to worker {task.assigned_to_worker!r}, which "
                f"is {'unlicensed' if worker else 'no longer registered'}. "
                f"Nothing on that host will run this {stack_op}.")
            stranded += 1

            stack_id = payload.get("stack_id")
            stack = (db.query(models.PersistentStack).get(stack_id)
                     if stack_id is not None else None)
            if stack is None:
                continue

            if stack_op in ("teardown", "migrate"):
                # Resources may survive on a host we can no longer reach. This
                # is exactly what cleanup_pending exists to record.
                stack.cleanup_pending = True
                stack.status_reason = (
                    f"{stack_op} could not be delivered: host "
                    f"{task.assigned_to_worker} is gone. Containers or volumes "
                    f"may still exist there.")
            elif stack_op == "deploy" and stack.status == "deploying":
                # Let it become a gap again so reconciliation can re-place it,
                # rather than leaving it wedged in `deploying` forever.
                # "unreachable" matches stack_reconciler.UNREACHABLE_STATUS (recovery path).
                stack.status = "unreachable"
                stack.status_reason = (
                    f"deploy could not be delivered: host "
                    f"{task.assigned_to_worker} is gone")

        if stranded:
            db.commit()
            logger.warning("stranded stack-task sweep: failed %d task(s) pinned "
                           "to workers that are gone", stranded)
    except Exception as e:  # noqa: BLE001
        logger.exception("stranded stack-task sweep raised: %s", e)
        db.rollback()
    finally:
        db.close()


def _run_stack_orphan_sweep():
    """Release accelerators held by stacks whose lifecycle op never finished.

    Committed capacity is derived from stack status (utils/accelerator_fit),
    which is what keeps reservations from drifting out of sync with reality —
    but it means a stack wedged in `deploying` or `migrating` holds its card
    forever. That happens when the backing task was cancelled, expired, or the
    worker died mid-op: nothing ever calls the completion handler, so the
    status never moves and the accelerator is stranded against a stack that is
    not running and never will.

    A stack is orphaned when it is in a transitional status and has NO task in
    flight for it. The grace period tolerates the ordinary window between
    dispatch and claim.
    """
    import models
    from datetime import timedelta

    db = _get_db()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STACK_ORPHAN_GRACE_MINUTES)
        stuck = (db.query(models.PersistentStack)
                 .filter(models.PersistentStack.status.in_(("deploying", "migrating")),
                         models.PersistentStack.updated_at <= cutoff)
                 .limit(200).all())
        released = 0
        for s in stuck:
            live = (db.query(models.ApiTaskQueue)
                    .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == s.id,
                            models.ApiTaskQueue.status.in_([
                                models.ApiTaskStatus.PENDING,
                                models.ApiTaskStatus.ASSIGNED,
                                models.ApiTaskStatus.CLAIMED,
                                models.ApiTaskStatus.RUNNING,
                            ]))
                    .count())
            if live:
                continue
            # "unreachable" matches stack_reconciler.UNREACHABLE_STATUS (recovery path).
            s.status = "unreachable"
            s.status_reason = (
                f"{'Deploy' if s.status == 'deploying' else 'Migrate'} never completed — no task "
                f"in flight after {STACK_ORPHAN_GRACE_MINUTES} min. The worker may have died "
                f"mid-operation. Any accelerator it was holding has been released."
            )
            s.cleanup_pending = True
            released += 1
        if released:
            db.commit()
            logger.warning("stack_orphan_sweep: released %d stranded stack(s)", released)
    except Exception as e:  # noqa: BLE001
        logger.warning("stack_orphan_sweep failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _zombie_run_reconcile_enabled() -> bool:
    """Master flag for the zombie-RUNNING reconcile — default ON (live).
    Platform setting ``scheduler.zombie_run_reconcile_enabled``, read at call
    time so an admin flip takes effect on the next tick without a restart."""
    return bool(_psr.get_setting("scheduler.zombie_run_reconcile_enabled"))


def _run_ghost_run_reconcile():
    """BUG.B85 — fail RUNNING runs that have no NodeRuns and no queued tasks.

    services.workflow_orchestrator.reconcile_ghost_runs does an atomic CAS per
    run and audits only applied flips; gated by orchestrator.ghost_run_reconcile_*.
    """
    db = _get_db()
    try:
        from services.workflow_orchestrator import reconcile_ghost_runs
        result = reconcile_ghost_runs(db)
        if result.get("reconciled"):
            logger.warning("ghost_run_reconcile: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.warning("ghost_run_reconcile failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_trigger_run_redispatch_sweep():
    """GO.6e — re-drive stranded trigger-fired runs every 5 minutes.

    dispatch_trigger_run commits a QUEUED run and dispatches its root tasks
    on a daemon thread; a uvicorn worker recycle in between orphans the run
    (0 NodeRuns, 0 tasks). This sweep (services.workflow_orchestrator.
    sweep_stranded_trigger_runs) re-runs the same dispatch after a short
    grace, capped at 2 attempts per run; after the cap the zombie-run
    reconcile finalizes the run. Self-gated by the
    orchestration.trigger_run_redispatch_* platform settings (default ON).
    """
    db = _get_db()
    try:
        from services.workflow_orchestrator import (
            sweep_stranded_trigger_runs,
        )
        result = sweep_stranded_trigger_runs(db)
        if result.get("re_dispatched"):
            logger.warning(
                "trigger_run_redispatch sweep: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.warning("trigger_run_redispatch sweep failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_zombie_run_reconcile():
    """Finalize zombie non-terminal workflow runs so status-keyed busy-checks are honest.

    A run is a ZOMBIE when it is in RUNNING/QUEUED/PENDING_ACTION, was started
    long enough ago to clear the ordinary completion window (grace, default
    30 min), has ZERO non-terminal NodeRuns, and has NO in-flight ApiTaskQueue
    row. Requiring zero non-terminal NodeRuns means there is no ready frontier to
    redrive (so this cannot re-trigger the workflow-completion orphan race — that
    bug was a QUEUED NodeRun being failed while a sibling could still dispatch it;
    here nothing is dispatchable). Status-only finalization + audit; billing /
    notification side-effects are intentionally NOT replayed (a zombie already
    missed them, and replaying risks double-charging).

    Default ON (live) via the ``scheduler.zombie_run_reconcile_enabled``
    platform setting; with it flipped off it logs what it WOULD finalize
    (DRY RUN) and mutates nothing.
    """
    import models
    from datetime import timedelta

    enabled = _zombie_run_reconcile_enabled()
    grace_min = int(os.getenv("STACK_ZOMBIE_RUN_GRACE_MINUTES", "30"))
    non_terminal_run = (models.TaskStatus.RUNNING, models.TaskStatus.QUEUED,
                        models.TaskStatus.PENDING_ACTION)
    non_terminal_node = (models.TaskStatus.QUEUED, models.TaskStatus.RUNNING,
                         models.TaskStatus.PENDING_ACTION)
    inflight_task = (models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
                     models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING)
    failed_node = (models.TaskStatus.FAILED, models.TaskStatus.FAILURE)

    db = _get_db()
    try:
        # GO.6e — before finalizing anything, give stranded QUEUED runs
        # (dispatch thread died before enqueuing a single task) one last
        # re-drive chance. The sweep is self-gated by its own platform
        # settings and attempt cap (no-op when off / at cap), so this only
        # ever turns a would-be zombie back into a RUNNING run. Runs
        # BEFORE the finalize loop so a run that can still run is driven,
        # not reaped.
        try:
            from services.workflow_orchestrator import (
                sweep_stranded_trigger_runs,
            )
            sweep_stranded_trigger_runs(db)
        except Exception as e:  # noqa: BLE001
            logger.warning("zombie_run_reconcile: pre-reconcile trigger "
                           "re-dispatch failed: %s", e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_min)
        # started_at is the only run timestamp available; a healthy run does not
        # sit with all-terminal nodes but a non-terminal status for `grace_min`.
        candidates = (db.query(models.WorkflowRun)
                      .filter(models.WorkflowRun.status.in_(non_terminal_run),
                              models.WorkflowRun.started_at.isnot(None),
                              models.WorkflowRun.started_at <= cutoff)
                      .limit(500).all())
        reconciled = 0
        handled_ids = set()  # BUG.B85b — the orphan branch below must not
                             # re-finalize a run the loop above already did
                             # (its status change is not yet committed, so the
                             # orphan scan's fresh SQL still sees it live).
        for run in candidates:
            # Any non-terminal NodeRun => the run may still legitimately progress.
            live_node = (db.query(models.NodeRun.id)
                         .filter(models.NodeRun.workflow_run_id == run.id,
                                 models.NodeRun.status.in_(non_terminal_node))
                         .first())
            if live_node is not None:
                continue
            # Any in-flight task => not a zombie (covers the dispatch->claim window
            # and system tasks tied to the run).
            live_task = (db.query(models.ApiTaskQueue.id)
                         .filter(models.ApiTaskQueue.workflow_run_id == run.id,
                                 models.ApiTaskQueue.status.in_(inflight_task))
                         .first())
            if live_task is not None:
                continue
            any_node = (db.query(models.NodeRun.id)
                        .filter(models.NodeRun.workflow_run_id == run.id)
                        .first())
            any_task = (db.query(models.ApiTaskQueue.id)
                        .filter(models.ApiTaskQueue.workflow_run_id == run.id)
                        .first())
            if any_node is None and any_task is None:
                # BUG.B85b — the run never dispatched a single node: marking it
                # COMPLETED is a false success (nothing ran).
                verdict = models.TaskStatus.FAILED
                orphaned = True
            else:
                any_failed = (db.query(models.NodeRun.id)
                              .filter(models.NodeRun.workflow_run_id == run.id,
                                      models.NodeRun.status.in_(failed_node))
                              .first()) is not None
                verdict = models.TaskStatus.FAILED if any_failed else models.TaskStatus.COMPLETED
                orphaned = False
            handled_ids.add(run.id)
            if not enabled:
                logger.info("zombie_run_reconcile [DRY RUN]: run %s (%s) would be "
                            "finalized -> %s (no live node/task, started %s)",
                            run.id, run.status, verdict.value, run.started_at)
                continue
            if orphaned:
                logger.warning("zombie reconcile: orphaned run %s -> FAILED", run.id)
            run.status = verdict
            if orphaned:
                run.error_message = "orphaned: no node runs"
            if getattr(run, "completed_at", None) is None:
                run.completed_at = datetime.now(timezone.utc)
            reason = ("reconciled from a stale non-terminal state: all nodes "
                      "terminal and no task in flight after %d min "
                      "(zombie-run reconcile)" % grace_min)
            if hasattr(run, "status_reason"):
                run.status_reason = reason
            try:
                db.add(models.AuditEvent(
                    tenant_id=getattr(run, "tenant_id", None),
                    user_id=None,
                    event_name="WORKFLOW_RUN_ZOMBIE_RECONCILED",
                    details={"workflow_run_id": run.id, "verdict": verdict.value,
                             "grace_minutes": grace_min},
                ))
            except Exception:  # noqa: BLE001 — audit is best-effort
                logger.debug("zombie_run_reconcile: audit append failed for run %s",
                             run.id, exc_info=True)
            reconciled += 1
        # BUG.B85b — orphan branch: a RUNNING/QUEUED run with ZERO NodeRuns
        # and ZERO ApiTaskQueue rows never dispatched anything, so the
        # started_at-keyed rescuers above are blind to it (a QUEUED run with
        # started_at NULL matches none of them) and it sits stranded forever.
        # Age comes from `created_at` when the column exists; until then
        # `started_at` is the only timestamp, and a QUEUED run with started_at
        # NULL is the documented stranded shape (GO.6c comment in
        # workflow_orchestrator.py), so NULL-age QUEUED rows are included.
        try:
            orphan_enabled = bool(_psr.get_setting(
                "orchestration.orphan_run_fail_enabled", db=db))
            orphan_min = max(1, int(_psr.get_setting(
                "orchestration.orphan_run_fail_minutes", db=db)))
        except Exception:  # noqa: BLE001 — registry miss => branch ON
            orphan_enabled, orphan_min = True, 45
        orphan_cutoff = datetime.now(timezone.utc) - timedelta(minutes=orphan_min)
        orphan_age_col = getattr(models.WorkflowRun, "created_at", None)
        if orphan_age_col is None:
            orphan_age_col = models.WorkflowRun.started_at
        if orphan_enabled:
            from sqlalchemy import and_ as _sand_, or_
            orphan_candidates = (db.query(models.WorkflowRun)
                                 .filter(models.WorkflowRun.status.in_(
                                     (models.TaskStatus.RUNNING,
                                      models.TaskStatus.QUEUED)),
                                         models.WorkflowRun.deleted_at.is_(None),
                                         # LDF-4 §7 — capacity-queued runs wait on
                                         # plan.max_concurrent_processes, they are
                                         # not orphans (same guard as the redispatch
                                         # sweep in workflow_orchestrator.py).
                                         ~models.WorkflowRun.is_capacity_queued.is_(True),
                                         or_(orphan_age_col <= orphan_cutoff,
                                             # NULL-age (started_at never stamped):
                                             # the documented stranded shape (run
                                             # 1535). Restricted to QUEUED — a
                                             # RUNNING run always has started_at
                                             # stamped, so NULL-age RUNNING rows
                                             # are a mid-dispatch race, not orphans.
                                             _sand_(models.WorkflowRun.status
                                                    == models.TaskStatus.QUEUED,
                                                    orphan_age_col.is_(None))))
                                 .limit(200).all())
            for run in orphan_candidates:
                if run.id in handled_ids:
                    continue
                if (db.query(models.NodeRun.id)
                     .filter(models.NodeRun.workflow_run_id == run.id)
                     .first()) is not None:
                    continue  # belongs to the existing logic above
                if (db.query(models.ApiTaskQueue.id)
                     .filter(models.ApiTaskQueue.workflow_run_id == run.id)
                     .first()) is not None:
                    continue
                if not enabled:
                    logger.info("zombie_run_reconcile [DRY RUN]: orphan run %s "
                                "(%s) would be finalized -> FAILED",
                                run.id, run.status)
                    continue
                logger.warning("zombie reconcile: orphaned run %s -> FAILED", run.id)
                run.status = models.TaskStatus.FAILED
                run.error_message = "orphaned: no node runs"
                if getattr(run, "completed_at", None) is None:
                    run.completed_at = datetime.now(timezone.utc)
                if hasattr(run, "status_reason"):
                    run.status_reason = (
                        "orphaned: no node runs and no task in flight after "
                        "%d min (zombie-run reconcile orphan branch)" % orphan_min)
                try:
                    db.add(models.AuditEvent(
                        tenant_id=getattr(run, "tenant_id", None),
                        user_id=None,
                        event_name="WORKFLOW_RUN_ZOMBIE_RECONCILED",
                        details={"workflow_run_id": run.id,
                                 "verdict": models.TaskStatus.FAILED.value,
                                 "orphaned": True,
                                 "orphan_minutes": orphan_min},
                    ))
                except Exception:  # noqa: BLE001 — audit is best-effort
                    logger.debug("zombie_run_reconcile: audit append failed for "
                                 "orphan run %s", run.id, exc_info=True)
                reconciled += 1

        # BUG.B85e — "run 2038" shape: NodeRuns EXIST (the orphan branch above
        # only matches zero NodeRuns, so it never sees these) but are ALL
        # stuck QUEUED and ZERO ApiTaskQueue rows were EVER created for the
        # run — not just none in flight, none ever — so nothing will ever
        # dispatch them. The main per-run loop above also never catches this:
        # its `live_node` check treats any non-terminal (incl. QUEUED)
        # NodeRun as "may still progress" and skips the run.
        try:
            q_enabled = bool(_psr.get_setting(
                "orchestration.queued_never_enqueued_fail_enabled", db=db))
            q_min = max(1, int(_psr.get_setting(
                "orchestration.queued_never_enqueued_fail_minutes", db=db)))
        except Exception:  # noqa: BLE001 — registry miss => branch ON
            q_enabled, q_min = True, 45
        if q_enabled:
            q_cutoff = datetime.now(timezone.utc) - timedelta(minutes=q_min)
            q_age_col = getattr(models.WorkflowRun, "created_at", None)
            if q_age_col is None:
                q_age_col = models.WorkflowRun.started_at
            from sqlalchemy import and_ as _qand_, or_ as _qor_
            q_candidates = (db.query(models.WorkflowRun)
                            .filter(models.WorkflowRun.status.in_(
                                (models.TaskStatus.RUNNING,
                                 models.TaskStatus.QUEUED)),
                                    models.WorkflowRun.deleted_at.is_(None),
                                    ~models.WorkflowRun.is_capacity_queued.is_(True),
                                    _qor_(q_age_col <= q_cutoff,
                                          _qand_(models.WorkflowRun.status
                                                 == models.TaskStatus.QUEUED,
                                                 q_age_col.is_(None))))
                            .limit(500).all())
            for run in q_candidates:
                if run.id in handled_ids:
                    continue
                # Zero ApiTaskQueue rows EVER (any status, not just in-flight)
                # — a single historical row means this run WAS dispatched at
                # some point and is a different failure shape.
                if (db.query(models.ApiTaskQueue.id)
                     .filter(models.ApiTaskQueue.workflow_run_id == run.id)
                     .first()) is not None:
                    continue
                node_rows = (db.query(models.NodeRun.id, models.NodeRun.status,
                                       models.NodeRun.started_at)
                             .filter(models.NodeRun.workflow_run_id == run.id)
                             .all())
                if not node_rows:
                    continue  # zero-NodeRun case — the orphan branch above owns this
                if any(nr.status != models.TaskStatus.QUEUED for nr in node_rows):
                    continue  # not the "all stuck QUEUED" shape
                # Guard: never touch a run with a recently-started NodeRun —
                # ambiguous (e.g. a retry mid-requeue), not a stale orphan.
                if any(nr.started_at is not None and nr.started_at > q_cutoff
                       for nr in node_rows):
                    continue
                handled_ids.add(run.id)
                if not enabled:
                    logger.info("zombie_run_reconcile [DRY RUN]: queued-never-"
                                "enqueued run %s (%s) would be finalized -> "
                                "FAILED (%d NodeRuns, 0 ApiTaskQueue rows ever)",
                                run.id, run.status, len(node_rows))
                    continue
                logger.warning("zombie reconcile: queued-never-enqueued run "
                                "%s -> FAILED", run.id)
                run.status = models.TaskStatus.FAILED
                run.error_message = "orphaned: node runs never enqueued"
                if getattr(run, "completed_at", None) is None:
                    run.completed_at = datetime.now(timezone.utc)
                if hasattr(run, "status_reason"):
                    run.status_reason = (
                        "orphaned: node runs never enqueued (%d NodeRuns all "
                        "QUEUED, zero ApiTaskQueue rows ever) after %d min "
                        "(zombie-run reconcile queued-never-enqueued branch)"
                        % (len(node_rows), q_min))
                try:
                    db.add(models.AuditEvent(
                        tenant_id=getattr(run, "tenant_id", None),
                        user_id=None,
                        event_name="WORKFLOW_RUN_ZOMBIE_RECONCILED",
                        details={"workflow_run_id": run.id,
                                 "verdict": models.TaskStatus.FAILED.value,
                                 "orphaned": True,
                                 "shape": "queued_never_enqueued",
                                 "node_run_count": len(node_rows),
                                 "grace_minutes": q_min},
                    ))
                except Exception:  # noqa: BLE001 — audit is best-effort
                    logger.debug("zombie_run_reconcile: audit append failed "
                                 "for queued-never-enqueued run %s", run.id,
                                 exc_info=True)
                reconciled += 1

        if reconciled:
            db.commit()
            logger.warning("zombie_run_reconcile: finalized %d zombie run(s)", reconciled)
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("zombie_run_reconcile failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_unrunnable_tasks_sweep():
    """Flag PENDING tasks whose gpu_variants don't intersect any active
    worker's hardware_tags so the Dashboard can surface "you need a
    new worker" banners instead of silently leaving tasks queued.

    Also covers the two requirement kinds added for GPU stacks —
    `accel_demand` (per-device VRAM fit) and `required_capabilities` — so an
    unplaceable stack reports WHY rather than sitting PENDING in silence, which
    is the same class of silent stall as the original stack lifecycle bug.

    A task is unrunnable when:
      - status = PENDING (not claimed yet, not assigned yet)
      - older than 10 minutes (tolerates startup lag / transient worker
        churn so we don't spam-flag tasks that will claim in 30s)
      - task_payload.gpu_variants is a non-empty object (absent means
        no routing constraint — any worker can run it)
      - NONE of the task_payload.gpu_variants keys match ANY active
        worker's hardware_tags

    Active worker = LICENSED, heartbeated within the last 5 minutes,
    not paused and not update_requested. Workers in drain/update
    shouldn't affect the flag either way.

    Flag clears automatically: when a matching worker joins and the
    task finally claims (or transitions out of PENDING for any
    reason), the claim SQL flips status != PENDING and the next
    sweep stops matching this row. We don't bother with a separate
    clear job.
    """
    import models
    from datetime import timedelta
    from sqlalchemy import text as sa_text

    db = _get_db()
    try:
        # Build the union of active workers' hardware_tags. Lowercased
        # for case-insensitive compare with the gpu_variants keys (same
        # normalisation as the claim-path filter in worker_tasks.py).
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        active_workers = db.query(models.WorkerLicense).filter(
            models.WorkerLicense.status == models.WorkerStatus.LICENSED,
            models.WorkerLicense.deleted_at.is_(None),
            models.WorkerLicense.paused.is_(False),
            models.WorkerLicense.update_requested.is_(False),
            models.WorkerLicense.last_heartbeat >= cutoff,
        ).all()
        active_tags: set[str] = set()
        # native-workers GP.3 — the set of OSes an active worker reports, for
        # the same "is anything actually going to pick this up" check as
        # active_tags above, but for native (execution: native) tasks, which
        # are gated by platform rather than hardware_tags.
        active_os: set[str] = set()
        for w in active_workers:
            hw = (w.hardware_specs or {}).get("hardware_tags") or []
            for t in hw:
                if isinstance(t, str):
                    active_tags.add(t.strip().lower())
            active_os.add(str((w.hardware_specs or {}).get("os") or "linux").strip().lower())

        # Walk PENDING tasks older than 10 min. LIMIT keeps the sweep
        # bounded on large queues; we'll catch the rest next tick.
        task_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        pending = db.query(models.ApiTaskQueue).filter(
            models.ApiTaskQueue.status == models.ApiTaskStatus.PENDING,
            models.ApiTaskQueue.created_at <= task_cutoff,
        ).limit(500).all()

        fleet = _fleet_devices(db, active_workers)

        flipped = 0
        for t in pending:
            payload = t.task_payload or {}
            if not isinstance(payload, dict):
                payload = {}
            reasons = []

            gv = payload.get("gpu_variants")
            if isinstance(gv, dict) and gv:
                keys = {str(k).strip().lower() for k in gv.keys()}
                if not (keys & active_tags):
                    missing = sorted(keys - active_tags)
                    reasons.append(
                        f"Waiting for a worker with hardware tag in "
                        f"{{{', '.join(missing)}}}. No active worker "
                        f"currently matches."
                    )

            # native-workers GP.3 — D-P2/D-P3: a native task names the platform
            # it needs (native_platform: windows/darwin/linux) and, per D-P2,
            # the app the recipe requires. Unplaceable reason names both, so
            # the Dashboard banner reads like "no online windows worker with
            # Blender" rather than a silent PENDING stall.
            if payload.get("execution") == "native":
                native_platform = str(payload.get("native_platform") or "").strip().lower()
                # GP.4 / D-P2 — requires_app {name, min_version} is the
                # structured form; native_app/required_app are the older
                # plain-string fields, kept for callers that pre-date GP.4.
                requires_app = payload.get("requires_app")
                if isinstance(requires_app, dict) and requires_app.get("name"):
                    app_name = str(requires_app["name"])
                    min_version = requires_app.get("min_version")
                    if min_version:
                        app_name = f"{app_name} >= {min_version}"
                else:
                    app_name = payload.get("native_app") or payload.get("required_app") or "the required app"
                if native_platform and native_platform not in active_os:
                    # BUG.B40f-p4 — an iOS mobile_build parked on the darwin/Metal
                    # lane (BUG.B40d/B40f) reads like "no online darwin worker with
                    # the required app" to an owner, which is not actionable. Name
                    # the Mac + Xcode in plain English, and — because the native
                    # backend's tenant gate (CORE/Worker/internal/executor/
                    # native_confine_policy.go tenantCodeGate) refuses TenantID>0
                    # work on a Mac unless worker.native_allow_unsandboxed_tenant_code
                    # is on — say so too when the task is a CUSTOMER tenant's.
                    # The rebuild path (utils/mobile_apk_build.submit_apk_rebuild
                    # via services/mobile_service.rebuild_app) carries the platform
                    # admin's tenant, i.e. the platform tenant (PLATFORM_TENANT_ID
                    # = 1, dependencies.py:1942), whose code is MOD's own app
                    # source: the gate's sentinel is TenantID==0 for platform work,
                    # but tenant 1 is never a customer, so no tenant sentence.
                    if native_platform == "darwin" and payload.get("node_kind") == "mobile_build":
                        _ios_reason = ("Waiting for a macOS worker with Xcode — "
                                       "iOS builds only run on a Mac.")
                    else:
                        _ios_reason = (
                            f"Waiting for an online {native_platform} worker with "
                            f"{app_name}. No active worker currently matches."
                        )
                    if native_platform == "darwin":
                        _tenant_id = getattr(t, "tenant_id", None) or 0
                        if _tenant_id > 1:  # 1 == platform tenant, never tenant code
                            try:
                                if not _psr.get_setting(
                                        "worker.native_allow_unsandboxed_tenant_code",
                                        db=db):
                                    _ios_reason += (
                                        " This Mac refuses tenant-supplied code until "
                                        "worker.native_allow_unsandboxed_tenant_code is on."
                                    )
                            except Exception:  # noqa: BLE001 — a reason never breaks the sweep
                                logger.debug("tenant-gate reason read failed for task %s",
                                             getattr(t, "id", None), exc_info=True)
                    reasons.append(_ios_reason)

            cap_reason = _capability_shortfall(payload.get("required_capabilities"), active_tags)
            if cap_reason:
                reasons.append(cap_reason)

            # GX.D1 — a placement-pinned task whose pinned worker/tag no
            # active worker satisfies reports the missing pin in the SAME
            # combined message (the "waiting for worker X" banner) instead
            # of sitting PENDING in silence — a pin is a hard constraint by
            # design (never run elsewhere), so the wait is expected and
            # must be legible.
            try:
                from services.workflow_placement import pinned_shortfall_reason
                pin_reason = pinned_shortfall_reason(payload, active_workers)
                if pin_reason:
                    reasons.append(pin_reason)
            except Exception:  # noqa: BLE001 — a reason must never break the sweep
                logger.debug("placement shortfall reason failed for task %s",
                             getattr(t, "id", None), exc_info=True)

            accel_reason = _accel_shortfall(payload.get("accel_demand") or {}, fleet)
            if accel_reason:
                reasons.append(accel_reason)

            # FX.131 (Q118=A) — a customer-training task under
            # training.own_workers_only whose tenant has no ACTIVE tenant-
            # owned worker is unrunnable (no platform/shared worker may
            # claim it — see the claim guard in
            # routers.worker_tasks._try_claim_task). The task stays PENDING
            # (fail-wait, never fail-route); this reason makes the Dashboard
            # banner say WHY instead of hanging silently.
            try:
                from services import platform_settings as _psr_fx131
                from routers.worker_tasks import _TRAINING_NODE_KINDS
                if (_psr_fx131.get_setting("training.own_workers_only", db=db)
                        and payload.get("node_kind") in _TRAINING_NODE_KINDS
                        and t.tenant_id is not None
                        and not any(
                            w.owner_type == "tenant_owned"
                            and w.owner_tenant_id == t.tenant_id
                            for w in active_workers
                        )):
                    reasons.append(
                        "Training tasks are restricted to this tenant's own "
                        "workers (training.own_workers_only). No active "
                        "tenant-owned worker exists for this tenant; the "
                        "task waits and will not run on a platform/shared "
                        "worker."
                    )
            except Exception:  # noqa: BLE001 — a reason must never break the sweep
                logger.debug("FX.131 own-workers-only reason failed for task %s",
                             getattr(t, "id", None), exc_info=True)

            # GY.C7 s2 (region allowlist) — a labelled task restricted to
            # regions no ACTIVE worker sits in is unrunnable: the claim
            # guard (routers.worker_tasks._label_region_guard_sql) withholds
            # it from every worker, so the Dashboard banner must say WHY
            # (region) instead of hanging silently — same fail-wait posture
            # as the pool/training reasons above.
            try:
                from utils.label_policy import worker_region_candidates
                _allowed = payload.get("label_regions")
                if isinstance(_allowed, list):
                    _allowed_norm = {str(r).strip().lower() for r in _allowed}
                    _worker_zones: set[str] = set()
                    for w in active_workers:
                        _worker_zones.update(
                            worker_region_candidates(w))
                    if not (_allowed_norm & _worker_zones):
                        reasons.append(
                            "Waiting for a worker in an allowed region "
                            f"(labels.allowed_regions allows "
                            f"{sorted(_allowed_norm)} for this task's "
                            "labels). No active worker carries a matching "
                            "'region:<zone>' tag; the task waits and will "
                            "not run on a worker outside the allowed "
                            "regions."
                        )
            except Exception:  # noqa: BLE001 — a reason must never break the sweep
                logger.debug("GY.C7 s2 region reason failed for task %s",
                             getattr(t, "id", None), exc_info=True)

            # gw-stack-networking GW.3 — a stack deploy whose FIXED host port is
            # in use on every eligible worker reports the port in the SAME
            # combined message as any other exclusion, instead of sitting
            # PENDING in silence (or failing at `compose up` "port is already
            # allocated"). `host: "any"` never blocks.
            if payload.get("stack_op") in ("deploy", "migrate") and payload.get("compose_spec"):
                try:
                    from services import stack_port_placement
                    port_reason = stack_port_placement.host_port_shortfall_reason(
                        db, payload.get("compose_spec"), active_workers,
                        exclude_stack_ids=[payload.get("stack_id")]
                        if payload.get("stack_id") is not None else None)
                    if port_reason:
                        reasons.append(port_reason)
                except Exception:  # noqa: BLE001 — a reason must never break the sweep
                    logger.debug("host-port shortfall reason failed for task %s",
                                 getattr(t, "id", None), exc_info=True)

            desired_unrunnable = bool(reasons)
            desired_reason = " ".join(reasons) if reasons else None
            if desired_unrunnable != t.unrunnable or (desired_unrunnable and t.unrunnable_reason != desired_reason):
                t.unrunnable = desired_unrunnable
                t.unrunnable_reason = desired_reason
                flipped += 1

        if flipped:
            db.commit()
            logger.info(
                "unrunnable_tasks_sweep: updated %d task(s); "
                "active_tags=%d", flipped, len(active_tags),
            )
    except Exception as e:
        db.rollback()
        logger.warning("unrunnable_tasks_sweep failed: %s", e)
    finally:
        db.close()


def _run_compliance_withheld_sweep():
    """COMPLIANCE-REFUSAL-ALERT (F-1): alert when a task a compliance claim
    guard withheld is STILL PENDING after compliance.withheld_stuck_minutes
    ("no worker satisfies the rule, it has not run"). Paid-only alert module
    (absent on CE = no-op); best-effort, one commit inside the module."""
    cra = optional_paid("services.compliance_refusal_alert")
    if cra is None:
        return
    db = _get_db()
    try:
        cra.sweep_stuck_withheld(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("compliance_withheld_sweep failed: %s", e)
    finally:
        db.close()


def _run_auto_retrain_eval(db=None, retrain_runner=None):
    """Decide whether the conditions for an automatic retrain are met,
    and if so fire it via the same pipeline POST /calibration/retrain
    uses.

    Two trigger conditions (OR'd):
      - recalibrations_since_last_train >= AUTO_RETRAIN_RECALIBRATION_THRESHOLD
      - (now - last_train_at) > AUTO_RETRAIN_SAFETY_NET_DAYS

    As of bucket-e Decision 8 the retrain runs as a worker task (image:
    `ghcr.io/modtim/ml-retrain:latest`), submitted via
    `services.ml_retrain.submit_retrain_run`. Submission returns
    immediately; the actual training completes hours later. To keep the
    counter-reset semantics correct without a task-completion hook, the
    tick checks at the start of each fire whether the most recent
    ml_retrain task completed successfully since `marker.last_train_at`
    and resets the counter accordingly.

    Acquires the same Redis lock (`mod:retrain:lock`) the manual endpoint
    uses so concurrent automatic+manual submissions don't double-queue.
    Lock TTL = RETRAIN_LOCK_TTL_SECONDS (4h default).

    Test injection points:
      - `db`: optional session (production callers pass nothing).
      - `retrain_runner`: optional callable replacing the real
        `submit_retrain_run` invocation, so tests can assert it was
        called without enqueuing a real task. Should return
        `(task_or_none, outcome_str)` shaped like submit_retrain_run.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import text

    threshold = int(os.getenv("AUTO_RETRAIN_RECALIBRATION_THRESHOLD", "3"))
    safety_days = int(os.getenv("AUTO_RETRAIN_SAFETY_NET_DAYS", "90"))

    owns_db = db is None
    if owns_db:
        db = _get_db()
    try:
        import models

        marker = db.query(models.PredictionTrainingMarker).filter(
            models.PredictionTrainingMarker.id == 1
        ).first()
        if marker is None:
            logger.info("auto_retrain_eval: no marker row yet — nothing to do")
            return False

        marker.last_check_at = datetime.now(timezone.utc)
        db.flush()

        now = datetime.now(timezone.utc)

        # Bucket-e Decision 8: observe completion of any recent ml_retrain
        # task and reset the counter if it succeeded since last marker
        # update. Replaces the old "synchronous run_retrain returned
        # status='success' → reset counter" semantic.
        last_observed = marker.last_train_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
        recent = (
            db.query(models.ApiTaskQueue)
            .filter(text("task_payload ->> 'node_kind' = 'ml_retrain'"))
            .filter(models.ApiTaskQueue.completed_at != None)
            .filter(models.ApiTaskQueue.completed_at > last_observed)
            .order_by(models.ApiTaskQueue.completed_at.desc())
            .first()
        )
        if recent is not None and recent.status == models.ApiTaskStatus.COMPLETED:
            payload = recent.result_payload or {}
            marker.recalibrations_since_last_train = 0
            marker.last_train_at = recent.completed_at
            marker.last_train_model_version = payload.get("model_version")

            # scheduler-promote-gate §2.3 — the trainer reports its gate
            # decision in the result payload. Mirror it into a
            # MODEL_PROMOTE_GATED audit event so an operator can grep
            # the audit log for promote decisions and inspect any
            # gated-to-shadow uploads in MinIO.
            gate_outcome = payload.get("gate_outcome")
            gate_details = payload.get("gate_details")
            if gate_outcome:
                try:
                    audit_details = {"outcome": gate_outcome}
                    if isinstance(gate_details, dict):
                        audit_details = {**gate_details, "outcome": gate_outcome}
                    db.add(models.AuditEvent(
                        tenant_id=None,
                        user_id=None,
                        event_name="MODEL_PROMOTE_GATED",
                        details=audit_details,
                    ))
                except Exception as audit_err:  # noqa: BLE001
                    logger.debug(
                        "MODEL_PROMOTE_GATED audit emit failed for task=%d: %s",
                        recent.id, audit_err,
                    )
            db.commit()
            logger.info(
                "auto_retrain_eval: observed completed retrain task=%d (v%s, gate=%s) — counter reset",
                recent.id, payload.get("model_version"), gate_outcome,
            )

        counter = marker.recalibrations_since_last_train or 0

        counter_hit = counter >= threshold
        safety_hit = False
        if marker.last_train_at:
            safety_hit = (now - marker.last_train_at) > timedelta(days=safety_days)
        else:
            safety_hit = counter > 0  # fresh DB: any data = retrain at least once

        if not (counter_hit or safety_hit):
            logger.info(
                "auto_retrain_eval: not triggering (counter=%d/%d, last_train=%s)",
                counter, threshold,
                marker.last_train_at.isoformat() if marker.last_train_at else "never",
            )
            db.commit()
            return False

        # Acquire retrain lock (same key the manual endpoint uses).
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        lock_acquired = False
        rdb = None
        try:
            import redis
            rdb = redis.from_url(redis_url)
            ttl = int(os.getenv("RETRAIN_LOCK_TTL_SECONDS", "14400"))
            lock_acquired = bool(rdb.set("mod:retrain:lock", "auto", nx=True, ex=ttl))
            if not lock_acquired:
                logger.info("auto_retrain_eval: retrain already in progress — skipping")
                db.commit()
                return False
        except Exception as e:
            logger.warning("auto_retrain_eval: Redis unavailable for lock — proceeding without: %s", e)

        # Submit the retrain task. Tests can swap this for a stub.
        if retrain_runner is None:
            def retrain_runner():
                from services.ml_retrain import submit_retrain_run
                return submit_retrain_run(
                    db, force=False, trigger_source="auto_retrain_eval",
                )

        logger.info(
            "auto_retrain_eval: SUBMITTING retrain (counter=%d/%d, last_train=%s, safety_hit=%s)",
            counter, threshold,
            marker.last_train_at.isoformat() if marker.last_train_at else "never",
            safety_hit,
        )

        try:
            result = retrain_runner()
            # New shape: (task_or_none, outcome). Old stubs that still
            # return a dict are tolerated for backwards compat — the
            # tests in tree mock this and we want to upgrade them
            # alongside but not require the full mock-API change in one
            # commit.
            if isinstance(result, tuple) and len(result) == 2:
                task, outcome = result
                logger.info(
                    "auto_retrain_eval: submission outcome=%s task_id=%s",
                    outcome, task.id if task is not None else None,
                )
                # Counter is NOT reset here. Decision 8 reset semantics:
                # the next tick observes completion via the recent-task
                # check at the top and resets the counter then. This
                # preserves "fire only when training actually succeeded"
                # without coupling the tick to task-completion hooks.
                db.commit()
                return outcome in ("submitted", "already_in_flight")
            else:
                # Legacy stub returning a dict — preserve old behaviour.
                status = (result or {}).get("status", "unknown")
                logger.info("auto_retrain_eval: legacy retrain finished status=%s", status)
                if status in ("deployed_shadow", "deployed", "success"):
                    marker.recalibrations_since_last_train = 0
                    marker.last_train_at = now
                    marker.last_train_model_version = (result or {}).get("version")
                    db.commit()
                else:
                    db.commit()
                return True
        except Exception as e:
            logger.error("auto_retrain_eval: retrain submission failed: %s", e, exc_info=True)
            db.commit()
            return False
        finally:
            if rdb is not None and lock_acquired:
                try:
                    rdb.delete("mod:retrain:lock")
                except Exception:
                    pass
    except Exception as e:
        if owns_db:
            db.rollback()
        logger.error("auto_retrain_eval failed: %s", e, exc_info=True)
        return False
    finally:
        if owns_db:
            db.close()


def _drift_scan_enabled() -> bool:
    return bool(_psr.get_setting("scheduler.drift_scan_enabled"))


def _run_drift_scan_tick():
    """Daily drift scan (scheduler-drift-recal-scan). No-op unless the
    scheduler.drift_scan_enabled setting is on — the scan queues real GPU
    calibration tasks."""
    if not _drift_scan_enabled():
        logger.info("scheduler_drift_scan: disabled — skipping")
        return None
    from services.scheduler_drift_scan import run_drift_scan
    db = _get_db()
    try:
        summary = run_drift_scan(db)
        logger.info("scheduler_drift_scan tick: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduler_drift_scan tick raised: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def _run_shadow_promote_sweep_s3(bucket: str = None, force_promote_after_h: float = None):
    """MinIO-backed shadow-promote sweep.

    The retrain-as-DAG path (bucket-e Decision 8) writes
    `scheduler-vN.json` shadow metadata to MinIO `platform-models/`,
    not to the API container's local filesystem. This sweep lists those
    objects, promotes ones past their probation window, demotes the
    previous production model, and writes updated metadata back to
    MinIO. Same idempotent semantics as the filesystem version.

    `force_promote_after_h`: when set, overrides each meta's
    `promote_after` value (used by ops to force immediate promotion of
    a fresh shadow model — e.g. for first-deploy verification).
    """
    from datetime import datetime, timezone
    import json
    import io

    bucket = bucket or os.getenv("MODEL_BUCKET", "platform-models")
    try:
        from utils.storage import get_service_client
        client = get_service_client()
    except Exception as e:
        logger.warning("shadow_promote_sweep_s3: MinIO client unavailable: %s", e)
        return 0

    try:
        if not client.bucket_exists(bucket):
            logger.debug("shadow_promote_sweep_s3: bucket %s missing", bucket)
            return 0
    except Exception as e:
        logger.warning("shadow_promote_sweep_s3: bucket check failed: %s", e)
        return 0

    metas = []
    try:
        for obj in client.list_objects(bucket, prefix="scheduler-v"):
            name = obj.object_name
            if not name.endswith(".json") or "normalization" in name:
                continue
            try:
                resp = client.get_object(bucket, name)
                meta = json.loads(resp.read().decode())
                resp.close()
                resp.release_conn()
            except Exception as e:
                logger.warning("shadow_promote_sweep_s3: skipping %s — %s", name, e)
                continue
            meta["_object_name"] = name
            metas.append(meta)
    except Exception as e:
        logger.warning("shadow_promote_sweep_s3: list failed: %s", e)
        return 0

    now = datetime.now(timezone.utc)
    current_production = next((m for m in metas if m.get("status") == "production"), None)

    promoted = 0
    for meta in metas:
        if meta.get("status") != "shadow":
            continue
        deployed_at_raw = meta.get("deployed_at")
        promote_after_h = (
            float(force_promote_after_h)
            if force_promote_after_h is not None
            else float(meta.get("promote_after", 24))
        )
        if not deployed_at_raw:
            continue
        try:
            deployed_at = datetime.fromisoformat(deployed_at_raw.replace("Z", "+00:00"))
            if deployed_at.tzinfo is None:
                deployed_at = deployed_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        age_h = (now - deployed_at).total_seconds() / 3600.0
        if age_h < promote_after_h:
            continue

        try:
            meta_pure = {k: v for k, v in meta.items() if not k.startswith("_")}
            meta_pure["status"] = "production"
            meta_pure["promoted_at"] = now.isoformat()
            data = json.dumps(meta_pure, indent=2, default=str).encode()
            client.put_object(bucket, meta["_object_name"], io.BytesIO(data), len(data))
            promoted += 1
            logger.info(
                "shadow_promote_sweep_s3: promoted v%s to production (age=%.1fh, promote_after=%sh)",
                meta.get("version"), age_h, promote_after_h,
            )
        except Exception as e:
            logger.warning("shadow_promote_sweep_s3: failed to promote v%s: %s", meta.get("version"), e)
            continue

        if current_production and current_production.get("version") != meta.get("version"):
            try:
                old_meta = {k: v for k, v in current_production.items() if not k.startswith("_")}
                old_meta["status"] = "deprecated"
                old_meta["deprecated_at"] = now.isoformat()
                data = json.dumps(old_meta, indent=2, default=str).encode()
                client.put_object(
                    bucket, current_production["_object_name"], io.BytesIO(data), len(data)
                )
                logger.info(
                    "shadow_promote_sweep_s3: deprecated v%s (replaced by v%s)",
                    current_production.get("version"), meta.get("version"),
                )
            except Exception as e:
                logger.warning("shadow_promote_sweep_s3: failed to deprecate v%s: %s",
                               current_production.get("version"), e)
        # Always advance current_production after a successful promotion,
        # even if there was no prior production. Prevents a second
        # eligible shadow in the same sweep from also reaching
        # status=production (we'd end up with two prods, no demote, since
        # the demote branch only fires when current_production is set).
        current_production = dict(meta_pure)
        current_production["_object_name"] = meta["_object_name"]

    if promoted == 0:
        logger.debug("shadow_promote_sweep_s3: nothing to promote")
    return promoted


def _evaluate_profile_staleness_threshold_days(profile, db) -> int:
    """Compute the effective staleness-threshold days for a HardwareProfile.

    Logic (composed from base ceiling + package-delta ratchet + air-gap exemption):

        ceiling   = CALIBRATION_STALENESS_DAYS (default 180)
        floor     = CALIBRATION_STALENESS_FLOOR_DAYS (default 30)
        slope     = CALIBRATION_STALENESS_RATCHET_SLOPE (default 0.75)

        For each WorkerLicense pointing at this profile:
          - if any worker reports air_gapped=True → return ceiling unconditional
            (air-gapped boxes don't change between calibrations; the package
             ratchet doesn't apply).
          - else compute package_delta = current.package_count - baseline.package_count
            (skip workers without a baseline yet — they haven't been calibrated
             so there's no delta to compute).
          - take max(package_delta) across all workers of this profile.

        threshold_days = clamp(ceiling - slope * max_delta, floor, ceiling)

    Worked example (defaults): 200-package delta → 180 - 0.75*200 = 30 → floor.
    Air-gapped or zero-delta worker → 180.
    """
    import models
    from services.platform_settings import get_setting as _psr_get_setting

    ceiling = int(os.getenv("CALIBRATION_STALENESS_DAYS", "180"))
    # GS.PSR.8 — the staleness FLOOR resolves through the
    # platform-settings registry (calibration.staleness_floor_days: DB >
    # env > 30), registered below. A bad value degrades to 30, matching
    # the legacy int-parse-failure behaviour for the same knob.
    try:
        floor = int(_psr_get_setting("calibration.staleness_floor_days", db=db))
    except (TypeError, ValueError):
        logger.warning("calibration.staleness_floor_days is not an integer; "
                       "defaulting to 30")
        floor = 30
    slope = float(os.getenv("CALIBRATION_STALENESS_RATCHET_SLOPE", "0.75"))

    workers = db.query(models.WorkerLicense).filter(
        models.WorkerLicense.hardware_profile_id == profile.id,
        models.WorkerLicense.deleted_at.is_(None),
    ).all()
    if not workers:
        return ceiling

    states = db.query(models.WorkerSystemState).filter(
        models.WorkerSystemState.worker_license_id.in_([w.id for w in workers])
    ).all()
    if not states:
        return ceiling

    if any(s.air_gapped for s in states):
        return ceiling

    deltas = []
    for s in states:
        if s.baseline_package_count is None:
            continue
        if s.package_count is None:
            continue
        delta = s.package_count - s.baseline_package_count
        if delta > 0:
            deltas.append(delta)
    if not deltas:
        return ceiling

    max_delta = max(deltas)
    effective = int(ceiling - slope * max_delta)
    if effective < floor:
        effective = floor
    if effective > ceiling:
        effective = ceiling
    return effective


def _run_calibration_staleness_eval(db=None):
    """Sweep HardwareProfile rows for time-based calibration drift.

    Runs daily at 5 UTC. For each profile with calibration_status='calibrated'
    and last_calibrated_at older than the effective threshold, flip
    calibration_status to 'stale'. Profiles that are 'uncalibrated',
    'calibrating', or already 'stale' are skipped — no point re-flipping.

    Driver-version-change detection runs inline in the heartbeat handler
    (crud.accelerator_cud.upsert_accelerators); this job is the
    time-based safety net for boxes whose drivers haven't actually
    changed but whose calibration data has aged out.

    Logs every transition. Idempotent — re-running on the same data
    after a flip is a no-op.

    The optional `db` arg lets tests inject their session-scoped fixture
    so the sweep sees uncommitted-to-prod test data; production callers
    pass nothing and a fresh SessionLocal is created/closed locally.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import and_

    owns_db = db is None
    if owns_db:
        db = _get_db()
    try:
        import models

        # Pre-filter at the FLOOR threshold — the per-profile effective
        # threshold can be as short as the floor when the package-delta
        # ratchet kicks in. A pre-filter at the ceiling would miss those.
        # We trade a slightly larger candidate set for not-missing-anything.
        # GS.PSR.8 — platform-settings registry (calibration.staleness_floor_days).
        from services.platform_settings import get_setting as _psr_get_setting
        floor_days = int(_psr_get_setting("calibration.staleness_floor_days", db=db))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=floor_days)

        candidates = db.query(models.HardwareProfile).filter(
            and_(
                models.HardwareProfile.calibration_status == "calibrated",
                models.HardwareProfile.last_calibrated_at != None,  # noqa: E711
                models.HardwareProfile.last_calibrated_at < cutoff,
            )
        ).all()

        if not candidates:
            logger.info(
                "calibration_staleness_eval: 0 profiles past %dd floor",
                floor_days,
            )
            return 0

        flipped = 0
        for profile in candidates:
            effective_threshold = _evaluate_profile_staleness_threshold_days(profile, db)
            profile_cutoff = now - timedelta(days=effective_threshold)
            if profile.last_calibrated_at < profile_cutoff:
                profile.calibration_status = "stale"
                flipped += 1
                logger.info(
                    "calibration_staleness_eval: hardware_profile=%s flipped to stale "
                    "(last_calibrated_at=%s, effective_threshold=%dd, age=%dd)",
                    profile.id,
                    profile.last_calibrated_at.isoformat() if profile.last_calibrated_at else "null",
                    effective_threshold,
                    (now - profile.last_calibrated_at).days,
                )

        if flipped:
            db.commit()
            logger.info(
                "calibration_staleness_eval: flipped %d/%d candidate profile(s) to stale",
                flipped, len(candidates),
            )
        else:
            logger.info(
                "calibration_staleness_eval: %d candidate profile(s) examined, 0 flipped",
                len(candidates),
            )
        return flipped
    except Exception as e:
        if owns_db:
            db.rollback()
        logger.error("calibration_staleness_eval failed: %s", e, exc_info=True)
        return 0
    finally:
        if owns_db:
            db.close()


def _run_sweep_stuck_harbor_scans():
    """Recovery sweep — finds NodeDefinition rows stuck in scanning /
    pending_scan and pushes them to a terminal scan_status.

    Why this exists: the eager daemon-thread poller in
    routers/node_definitions._poll_harbor_scan_async is fragile. It
    dies silently if the kicker process exits (e.g. a short-lived
    rescan call), and any unhandled exception inside the worker
    function strands the row at `scanning` forever. This sweep runs
    every minute, queries tenant Harbor for the current scan_overview
    of each stuck row, and applies the same outcome-decision rule via
    the shared `_decide_harbor_scan_outcome` helper.

    Per-row timeout: 15 minutes from `scan_result.poll_started_at`.
    Beyond that, we write `scan_failed: stuck_in_scanning_*` so the
    dashboard doesn't show an indefinite spinner.

    Idempotent — re-running on the same set is a no-op (rows that
    finished in the previous tick are no longer in the stuck set).
    """
    from datetime import datetime, timezone, timedelta
    db = _get_db()
    try:
        import models
        from routers.node_definitions import (
            _decide_harbor_scan_outcome,
            _persist_scan_outcome,
            _image_is_in_tenant_harbor,
            _parse_tenant_harbor_ref,
        )

        stuck = db.query(models.NodeDefinition).filter(
            models.NodeDefinition.scan_status.in_(["scanning", "pending_scan"]),
            models.NodeDefinition.executable_path.isnot(None),
            models.NodeDefinition.tenant_id.isnot(None),
        ).all()

        if not stuck:
            return

        # Lazy-import the Harbor client so the scheduler still boots
        # if utils.tenant_harbor_client is broken — we'd rather log
        # the per-tick error than crash the whole scheduler loop.
        try:
            from utils.tenant_harbor_client import TenantHarborClient, TenantHarborError
            harbor = TenantHarborClient()
        except Exception as e:
            logger.warning("sweep_stuck_harbor_scans: Harbor client init failed: %s", e)
            return

        max_age = timedelta(minutes=15)
        now = datetime.now(timezone.utc)
        terminal_count = 0
        timeout_count = 0
        still_pending = 0
        for nd in stuck:
            if not _image_is_in_tenant_harbor(nd.executable_path):
                continue
            repo_ref = _parse_tenant_harbor_ref(nd.executable_path)
            if not repo_ref or not repo_ref[0]:
                continue
            repo, reference = repo_ref

            # Per-row poll: ask Harbor where it's at NOW.
            try:
                overview = harbor.get_scan_status(nd.tenant_id, repo, reference)
            except TenantHarborError as e:
                logger.debug("sweep: harbor poll error for node %d: %s", nd.id, e)
                continue
            except Exception as e:
                logger.warning("sweep: unexpected harbor error for node %d: %s", nd.id, e)
                continue

            outcome, detail = _decide_harbor_scan_outcome(overview)
            if outcome is not None:
                _persist_scan_outcome(nd.id, outcome, detail)
                terminal_count += 1
                continue

            # Harbor still pending. Check how long we've been stuck and
            # apply the per-row timeout if it's been too long.
            poll_started_at = None
            sr = nd.scan_result if isinstance(nd.scan_result, dict) else {}
            ps_iso = sr.get("poll_started_at") if isinstance(sr, dict) else None
            if ps_iso:
                try:
                    poll_started_at = datetime.fromisoformat(ps_iso.replace("Z", "+00:00"))
                except Exception:
                    poll_started_at = None
            if poll_started_at and (now - poll_started_at) > max_age:
                _persist_scan_outcome(
                    nd.id, "scan_failed",
                    f"Stuck in scanning for >{max_age.total_seconds():.0f}s "
                    "(Harbor never reported terminal status)",
                )
                timeout_count += 1
            else:
                still_pending += 1

        if terminal_count or timeout_count:
            logger.info(
                "sweep_stuck_harbor_scans: %d resolved → terminal, "
                "%d timed out, %d still pending",
                terminal_count, timeout_count, still_pending,
            )
    except Exception as e:
        logger.warning("sweep_stuck_harbor_scans failed: %s", e)
    finally:
        db.close()


def _run_heal_node_scan_status():
    """Re-run the platform-node scan_status heal that database.py runs
    at startup, on a 5-minute interval. Closes the gap where rows can
    re-flip to pending_scan / scanning / failed post-startup (e.g.
    during a transient registry outage) and stay stale until the next
    API restart. Idempotent — UPDATE with no matching rows is a no-op.

    Logs only when rowcount > 0 so the steady-state case is silent.
    A non-zero count is a useful signal that something upstream is
    mis-flagging rows; surface loudly so it's investigated, not buried.
    """
    db = _get_db()
    try:
        from sqlalchemy import text
        own = db.execute(text(
            "UPDATE node_definitions "
            "SET scan_status = 'approved' "
            "WHERE (tenant_id IS NULL OR name LIKE 'system.' || '%') "
            "AND scan_status IN ('pending_scan', 'scanning', 'failed')"
        ))
        clones = db.execute(text(
            "UPDATE node_definitions tn "
            "SET scan_status = 'approved' "
            "FROM node_definitions sn "
            "WHERE sn.tenant_id IS NULL "
            "AND sn.scan_status = 'approved' "
            "AND tn.tenant_id IS NOT NULL "
            "AND tn.name = sn.name "
            "AND tn.scan_status IN ('pending_scan', 'scanning', 'failed')"
        ))
        db.commit()
        if (own.rowcount or 0) + (clones.rowcount or 0) > 0:
            logger.warning(
                "heal_node_scan_status: re-approved %d own rows + %d tenant clones — "
                "investigate what upstream is flipping these post-startup",
                own.rowcount or 0, clones.rowcount or 0,
            )
    except Exception as e:
        db.rollback()
        logger.warning("heal_node_scan_status failed: %s", e)
    finally:
        db.close()


# =========================================================================
# Wizard-rebuild §11b — retention hard-delete sweep
# =========================================================================

_GC_BATCH_LIMIT_PER_TABLE = int(
    os.getenv("MOD_RETENTION_GC_BATCH_LIMIT", "500"),
)

_GC_TABLE_SPECS = (
    ("node_definition", "NodeDefinition", "tenant_id"),
    ("workflow_run",    "WorkflowRun",    "tenant_id"),
    ("build_spec",      "BuildSpecRecord", "tenant_id"),
    ("repo_docs_extract", "RepoDocsExtract", "tenant_id"),
)


def _resolve_audit_actor_for_tenant(db, tenant_id: int) -> int:
    """Pick a real user id under whose authority the GC's audit events
    will be recorded. Preference: the tenant's first platform admin,
    then any non-deleted user in the tenant, then the platform tenant's
    first admin. Returns 0 if no candidate exists (caller treats 0 as
    'skip audit')."""
    try:
        import models
        admin = (
            db.query(models.User)
            .filter(
                models.User.tenant_id == tenant_id,
                models.User.is_platform_admin.is_(True),
            )
            .order_by(models.User.id)
            .first()
        )
        if admin is not None:
            return int(admin.id)
        any_user = (
            db.query(models.User)
            .filter(models.User.tenant_id == tenant_id)
            .order_by(models.User.id)
            .first()
        )
        if any_user is not None:
            return int(any_user.id)
        platform_admin = (
            db.query(models.User)
            .filter(models.User.is_platform_admin.is_(True))
            .order_by(models.User.id)
            .first()
        )
        if platform_admin is not None:
            return int(platform_admin.id)
    except Exception as e:
        logger.warning("§11b: actor resolution failed for tenant %s: %s", tenant_id, e)
    return 0


def _gc_query_expired_rows(db, model, tenant_retention_days_by_id: dict):
    """Return soft-deleted rows whose deleted_at is older than their
    tenant's retention_days. Bypasses the loader-criteria filter so
    we can actually see the tombstones.
    """
    from sqlalchemy import or_
    import models
    tenant_clauses = []
    for tid, rdays in tenant_retention_days_by_id.items():
        cutoff = datetime.now(timezone.utc) - _retention_timedelta(rdays)
        tenant_clauses.append(
            (model.tenant_id == tid) & (model.deleted_at < cutoff)
        )
    if not tenant_clauses:
        return []
    q = (
        db.query(model)
        .filter(model.deleted_at.isnot(None))
        .filter(or_(*tenant_clauses))
        .order_by(model.deleted_at.asc())
        .limit(_GC_BATCH_LIMIT_PER_TABLE)
        .execution_options(include_deleted=True)
    )
    return q.all()


def _retention_timedelta(days: int):
    from datetime import timedelta
    return timedelta(days=int(days))


def _gc_reclaim_harbor_for_node(node) -> None:
    """Best-effort Harbor artifact delete for a soft-deleted node.
    Failure logged + swallowed; the DB row hard-delete still proceeds.
    """
    image_ref = (node.executable_path or "").strip()
    tenant_id = node.tenant_id
    if not image_ref or not tenant_id:
        return
    try:
        from routers.node_definitions import _parse_harbor_image_ref
        repo, ref = _parse_harbor_image_ref(image_ref)
    except Exception:
        repo, ref = "", ""
    if not repo or not ref:
        return
    try:
        from utils.tenant_harbor_client import TenantHarborClient
        TenantHarborClient().delete_artifact(
            tenant_id=tenant_id, repository=repo, reference=ref,
        )
    except Exception as e:
        logger.info(
            "§11b GC Harbor delete skipped for %s (tenant %s): %s",
            image_ref, tenant_id, e,
        )


def _run_hard_delete_after_retention():
    """§11b — daily 02:00 UTC sweep. For each tenant: read
    retention_days; for each of the 5 wizard-soft-delete tables, find
    rows whose ``deleted_at`` is older than retention; reclaim Harbor
    artifacts (for NodeDefinitions); hard-delete the row; emit
    ``CONTENT_RETENTION_EXPIRED`` audit event.

    Every tenant's effective retention is capped at <=90 days regardless of
    its configured ``retention_days``. The same window governs
    ``node_source_revision`` tombstones (see
    ``services/node_source_revisions.py::prune_expired``), which this
    sweep also purges.

    Bounded to ``_GC_BATCH_LIMIT_PER_TABLE`` rows per table per run;
    long backlogs trickle through over multiple days. Idempotent
    across re-runs: rows already hard-deleted are simply absent on
    the next pass.
    """
    if iso_mode_on():
        logger.info("hard_delete_after_retention: ISO mode on — soft-deleted rows are kept, nothing purged")
        return
    db = _get_db()
    try:
        import models, audit
        from services import node_source_revisions

        tenants = (
            db.query(models.Tenant)
            .filter(models.Tenant.deleted_at.is_(None))
            .execution_options(include_deleted=True)
            .all()
        )
        retention_by_id = {int(t.id): int(t.retention_days or 90) for t in tenants}
        if not retention_by_id:
            logger.debug("§11b hard_delete_after_retention: no active tenants")
            return

        # Community Edition: every tenant's retention is capped at 90 days.
        retention_by_id = {tid: min(rdays, 90) for tid, rdays in retention_by_id.items()}

        # GY.C15 — legal hold / preserve mode: rows belonging to a held
        # tenant are skipped so retention cannot purge preserved data
        # (supports preservation requirements, e.g. DFARS 252.204-7012
        # 90-day). Resolved ONCE for the sweep; held_tenants() is fail
        # closed (a lookup error => every tenant treated as held).
        # GY.V7 — audit-scoped holds (e-discovery / audit-record holds)
        # additionally freeze the tenant's retention GC; held_audit_
        # tenants() is fail closed too. GY.V7 run-scoped holds freeze
        # the held run's own tombstone (per-row check below).
        from utils.paid_import import optional_paid
        _lhold = optional_paid("services.legal_hold")
        if _lhold is None:  # FX.162.p5 — CE carve: no legal holds exist
            lh_platform_wide, lh_tenant_ids = False, set()
            lh_aud_wide, lh_aud_tids = False, set()
        else:
            lh_platform_wide, lh_tenant_ids = _lhold.held_tenants(db)
            lh_aud_wide, lh_aud_tids = _lhold.held_audit_tenants(db)
        lh_platform_wide = lh_platform_wide or lh_aud_wide
        lh_tenant_ids = lh_tenant_ids | lh_aud_tids
        # GY.V7 — run-scoped hold set for the workflow_run table.
        # held_run_ids() is fail closed: a lookup error yields None, in
        # which case NOTHING is purged this sweep (a broken hold lookup
        # must never turn into an unpreserved purge).
        lh_run_ids = (set() if _lhold is None
                      else _lhold.held_run_ids(db))
        if lh_run_ids is None:
            logger.info(
                "§11b GC: run-hold lookup failed — failing closed "
                "(nothing purged this sweep); rows left in place"
            )
            return

        tables = (
            (models.NodeDefinition,         "node_definition"),
            (models.WorkflowRun,             "workflow_run"),
            (models.BuildSpecRecord,         "build_spec"),
            (models.RepoDocsExtract,         "repo_docs_extract"),
        )
        total_purged = 0
        for model, resource_type in tables:
            try:
                rows = _gc_query_expired_rows(db, model, retention_by_id)
            except Exception as e:
                logger.warning("§11b GC scan failed for %s: %s", resource_type, e)
                continue
            for row in rows:
                row_id = int(getattr(row, "id"))
                tenant_id = int(getattr(row, "tenant_id"))
                # GY.C15 — skip held tenants (fail closed above).
                if lh_platform_wide or tenant_id in lh_tenant_ids:
                    logger.info(
                        "§11b GC: skipping %s id=%s (tenant %s) — legal "
                        "hold active; row left in place",
                        resource_type, row_id, tenant_id,
                    )
                    continue
                # GY.V7 — a run-scoped hold freezes that run's own
                # tombstone (lh_run_ids resolved fail-closed above).
                if resource_type == "workflow_run" and row_id in lh_run_ids:
                    logger.info(
                        "§11b GC: skipping %s id=%s (tenant %s) — "
                        "run-scoped legal hold active; row left in "
                        "place",
                        resource_type, row_id, tenant_id,
                    )
                    continue
                if resource_type == "node_definition":
                    _gc_reclaim_harbor_for_node(row)
                actor = _resolve_audit_actor_for_tenant(db, tenant_id)
                try:
                    # ISO-exception: transient retention GC — this row is ALREADY
                    # soft-deleted and has aged past the retention window; this is
                    # the periodic maintenance purge of expired tombstones,
                    # hard-delete required (audited via log_hard_delete_retention).
                    db.delete(row)
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning(
                        "§11b GC hard-delete failed for %s id=%s: %s",
                        resource_type, row_id, e,
                    )
                    continue
                if actor:
                    audit.log_hard_delete_retention(
                        db=db,
                        actor_user_id=actor,
                        tenant_id=tenant_id,
                        resource_type=resource_type,
                        resource_id=row_id,
                        metadata={
                            "retention_days": retention_by_id.get(tenant_id),
                        },
                    )
                else:
                    logger.warning(
                        "§11b GC: no audit actor for tenant %s (%s id=%s) — "
                        "row hard-deleted, audit row skipped",
                        tenant_id, resource_type, row_id,
                    )
                total_purged += 1

        # NodeVariantVerification has no tenant_id column; resolve
        # tenant via the linked NodeDefinition (or BuildSpec) and
        # apply the same retention rule.
        _gc_node_variant_verification(
            db, retention_by_id,
            skip_tenant=lambda tid: (
                lh_platform_wide or (tid is not None and int(tid) in lh_tenant_ids)
            ),
        )

        # open-core-tier-structure — node_source_revision tombstones
        # (delete_revision-set deleted_at). Fixed 90-day window rather
        # than per-tenant retention_days (it isn't a per-tenant
        # soft-delete table).
        try:
            purged_revisions = node_source_revisions.prune_expired(
                db, days=90,
                skip_tenant=lambda tid: (
                    lh_platform_wide or (tid is not None and int(tid) in lh_tenant_ids)
                ),
            )
        except Exception as e:
            logger.warning("§11b GC scan failed for node_source_revision: %s", e)
            purged_revisions = []
        for rev in purged_revisions:
            tenant_id = rev.get("tenant_id")
            if tenant_id is None:
                # System/tenant-less node revision — nothing to attribute
                # the audit event to; the row is still purged.
                continue
            actor = _resolve_audit_actor_for_tenant(db, int(tenant_id))
            if actor:
                audit.log_hard_delete_retention(
                    db=db,
                    actor_user_id=actor,
                    tenant_id=int(tenant_id),
                    resource_type="node_source_revision",
                    resource_id=int(rev["id"]),
                    metadata={"retention_days": 90},
                )
            else:
                logger.warning(
                    "§11b GC: no audit actor for tenant %s (node_source_revision "
                    "id=%s) — row hard-deleted, audit row skipped",
                    tenant_id, rev["id"],
                )
        total_purged += len(purged_revisions)
        if purged_revisions:
            logger.info(
                "§11b hard_delete_after_retention: purged %d node_source_revision "
                "row(s)",
                len(purged_revisions),
            )

        if total_purged:
            logger.info(
                "§11b hard_delete_after_retention: purged %d row(s) total",
                total_purged,
            )
    finally:
        db.close()


def _gc_node_variant_verification(db, retention_by_id: dict,
                                  skip_tenant=None) -> None:
    """Variant-verification rows don't carry a tenant_id directly.
    Resolve via the parent NodeDefinition (preferred) or BuildSpec,
    then apply the same per-tenant retention rule."""
    import models, audit
    try:
        from sqlalchemy import or_
        candidates = (
            db.query(models.NodeVariantVerification)
            .filter(models.NodeVariantVerification.deleted_at.isnot(None))
            .order_by(models.NodeVariantVerification.deleted_at.asc())
            .limit(_GC_BATCH_LIMIT_PER_TABLE)
            .execution_options(include_deleted=True)
            .all()
        )
    except Exception as e:
        logger.warning("§11b GC scan failed for node_variant_verification: %s", e)
        return

    for v in candidates:
        try:
            tid = None
            if v.node_definition_id is not None:
                nd = (
                    db.query(models.NodeDefinition)
                    .filter(models.NodeDefinition.id == v.node_definition_id)
                    .execution_options(include_deleted=True)
                    .first()
                )
                if nd is not None:
                    tid = nd.tenant_id
            if tid is None and v.build_spec_id is not None:
                bs = (
                    db.query(models.BuildSpecRecord)
                    .filter(models.BuildSpecRecord.id == v.build_spec_id)
                    .execution_options(include_deleted=True)
                    .first()
                )
                if bs is not None:
                    tid = bs.tenant_id
            if tid is None:
                continue
            rdays = retention_by_id.get(int(tid))
            if rdays is None:
                continue
            cutoff = datetime.now(timezone.utc) - _retention_timedelta(rdays)
            if v.deleted_at is None or v.deleted_at >= cutoff:
                continue
            # GY.C15 — legal hold: skip held tenants (fail closed in
            # caller; skip_tenant returns True when the tenant is held).
            if skip_tenant is not None and skip_tenant(tid):
                logger.info(
                    "§11b GC: skipping node_variant_verification id=%s "
                    "(tenant %s) — legal hold active; row left in place",
                    v.id, tid,
                )
                continue
            actor = _resolve_audit_actor_for_tenant(db, int(tid))
            row_id = int(v.id)
            # ISO-exception: transient retention GC — this row is ALREADY
            # soft-deleted (deleted_at older than the per-tenant retention
            # cutoff); periodic maintenance purge of expired tombstones,
            # hard-delete required (audited via log_hard_delete_retention below).
            db.delete(v)
            db.commit()
            if actor:
                audit.log_hard_delete_retention(
                    db=db,
                    actor_user_id=actor,
                    tenant_id=int(tid),
                    resource_type="node_variant_verification",
                    resource_id=row_id,
                    metadata={"retention_days": rdays},
                )
        except Exception as e:
            db.rollback()
            logger.warning(
                "§11b GC variant_verification hard-delete failed: %s", e,
            )


# ---------------------------------------------------------------------------
# GSEC FX.120 p2c (owner decision Q107=A) — the ELEVATION AUTO-REVOKE tick.
# services/privileged_elevation_approval.sweep_expired() expires every live
# elevation row past its deadline: a 'pending' row past its approval window
# (nobody decided it -> it is NOT a grant) and an 'approved'/'active' row
# past expires_at (the TTL clock — the owner decision's "default 2 h, then
# revoked automatically"). expire_lapsed writes the PRIVILEGED_ELEVATION_EXPIRED
# audit row itself, and every row that leaves 'active' goes to
# on_elevation_ended (the p3a/p3b capability revoke, a no-op until those land).
# security.elevation_expiry_check_seconds is re-read EVERY pass, so turning
# the sweep on/off needs no restart — but OFF means NO auto-revoke, which is
# the guarantee the whole decision rests on, so it is a deliberate operator
# choice. NOTHING here may raise into APScheduler: an unhandled raise is
# exactly the "stops firing jobs" symptom start_scheduler_watchdog recovers
# from, and a broken elevation sweep must not fence the other jobs.
# ---------------------------------------------------------------------------
def _fx120_elevation_expiry_tick():
    """One scheduled elevation auto-revoke pass (job id fx120_elevation_expiry)."""
    db = _get_db()
    try:
        from services import privileged_elevation_approval as _fx120
        cadence = int(_psr.get_setting(
            "security.elevation_expiry_check_seconds"))
    except Exception as e:  # noqa: BLE001 — settings read failure: keep sweeping
        logger.warning("fx120_elevation_expiry tick: settings read failed: %s", e)
        cadence = 60
    if cadence <= 0:
        logger.info("fx120_elevation_expiry tick: disabled "
                    "(security.elevation_expiry_check_seconds 0) — no-op")
        return None
    try:
        counters = _fx120.sweep_expired(db)
        if counters.get("expired") or counters.get("ended"):
            logger.info("fx120_elevation_expiry tick: %s", counters)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx120_elevation_expiry tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.120 p5b (owner Q107=A) — the WEEKLY ELEVATION SUMMARY tick.
# Same shape as _fx120_elevation_expiry_tick: a fresh session, the gate
# (elevation.weekly_summary_enabled + a re-read of elevation.weekly_summary_
# weekday / elevation.weekly_summary_hour_utc) EVERY pass, the work entirely
# inside services/privileged_elevation_summary, and NOTHING may raise into
# APScheduler — an unhandled raise is exactly the "stops firing jobs" symptom
# start_scheduler_watchdog recovers from, and a summary send must never fence
# the elevation auto-revoke sweep or the rest of the fleet. Idempotent per
# ISO week: the send's ADMIN_SYSTEM_MAINTENANCE audit row is the dedupe record, so a
# restart or a double-fire in the same week is a no-op (no second email,
# no second audit row).
# ---------------------------------------------------------------------------
def _fx120_weekly_elevation_summary_tick():
    """One scheduled weekly-elevation-summary pass (job id
    fx120_weekly_elevation_summary)."""
    db = _get_db()
    try:
        from services import privileged_elevation_summary as _fx120p5b
        outcome = _fx120p5b.weekly_summary_tick(db)
        if outcome.get("ran"):
            logger.info("fx120_weekly_elevation_summary tick: %s", outcome)
        else:
            logger.info("fx120_weekly_elevation_summary tick: skipped (%s)",
                        outcome.get("reason"))
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx120_weekly_elevation_summary tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.117 p4b (owner decision Q104=A) — the SECRET EXPIRY REMINDER tick.
# Same shape as _fx120_elevation_expiry_tick: a fresh session, the work
# entirely inside services/secret_expiry_service.py, and NOTHING may raise
# into APScheduler — an unhandled raise is exactly the "stops firing jobs"
# symptom start_scheduler_watchdog recovers from, and a broken reminder send
# must not fence the p4a expiry enforcement, the p2 expiry sweep or the rest
# of the fleet. The cadence is a fixed hourly heartbeat: the exactly-once
# record is the live `secret_expiries` row's last_reminded_at stamp (read
# inside the tick against secrets.expiry_reminder_interval_hours), so a
# restart or a double-fire in the same window is a no-op — no second email,
# no second audit row.
# ---------------------------------------------------------------------------
def _fx117_secret_expiry_reminders_tick():
    """One scheduled secret-expiry-reminder pass (job id
    fx117_secret_expiry_reminders)."""
    db = _get_db()
    try:
        from services import secret_expiry_service as _fx117p4
        result = _fx117p4.expiry_reminder_tick(db)
        if result.get("sent"):
            logger.info("fx117_secret_expiry_reminders tick: %s", result)
        else:
            logger.info("fx117_secret_expiry_reminders tick: nothing due (%s)",
                        result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx117_secret_expiry_reminders tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.133 p2b (owner Q121=A) — the DSR DEADLINE SWEEP tick.
# Per-state deadline timers are platform settings registered in-module in
# services/dsr_requests.py; the sweep itself lives there too (deadline_sweep).
# The tick never raises into APScheduler (that is exactly the "stops firing
# jobs" symptom start_scheduler_watchdog recovers from), and the sweep is
# idempotent: the audit trail is the exactly-once record, so a second pass
# over the same request writes nothing.
# ---------------------------------------------------------------------------


def _fx133_dsr_deadline_sweep_tick():
    """One scheduled DSR deadline pass (job id fx133_dsr_deadline_sweep)."""
    db = _get_db()
    try:
        from services.dsr_requests import deadline_sweep as _dsr_sweep
        result = _dsr_sweep(db)
        if result.get("overdue_flagged") or result.get("due_soon"):
            logger.info("fx133_dsr_deadline_sweep tick: %s", result)
        else:
            logger.info("fx133_dsr_deadline_sweep tick: nothing due (%s)",
                        result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx133_dsr_deadline_sweep tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.143 p1a (owner Q130=A) — the CAPACITY-ALERT SWEEP tick.
# GPU-pool capacity signals (queue claim-wait + unclaimed backlog,
# sustained GPU utilization) with thresholds as platform settings
# registered in-module in services/capacity_alerts.py; the sweep itself
# lives there too (evaluate). The tick never raises into APScheduler
# (that is exactly the "stops firing jobs" symptom start_scheduler_watchdog
# recovers from), and the sweep is idempotent: the audit trail is the
# dedupe record, so a second pass over the same breach within
# capacity_alerts.dedupe_hours writes nothing.
# ---------------------------------------------------------------------------


def _fx143_capacity_alert_sweep_tick():
    """One scheduled capacity-alert pass (job id fx143_capacity_alert_sweep)."""
    db = _get_db()
    try:
        from services.capacity_alerts import evaluate as _cap_eval
        result = _cap_eval(db)
        if result.get("breached"):
            logger.info("fx143_capacity_alert_sweep tick: %s breached (%s)",
                        result["breached"], result["signals"])
        else:
            logger.info("fx143_capacity_alert_sweep tick: pool within "
                        "thresholds (%s)", result["signals"])
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx143_capacity_alert_sweep tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.136 p2 (owner Q124=A, colo-corrected) — the GPU-TEMPERATURE-ALERT
# SWEEP tick. Thresholds (warn/critical °C + the sustained-minutes floor) are
# platform settings registered IN-MODULE in services/gpu_temperature_alerts.py;
# the sweep itself lives there too (evaluate). The tick never raises into
# APScheduler (that is exactly the "stops firing jobs" symptom
# start_scheduler_watchdog recovers from), and the sweep is idempotent: the
# audit trail is the dedupe record, so a second pass over the same sustained
# breach within gpu_temperature_alerts.dedupe_hours writes nothing.
# ---------------------------------------------------------------------------


def _fx136_gpu_temperature_alert_sweep_tick():
    """One scheduled GPU temperature-alert pass (job id
    fx136_gpu_temperature_alert_sweep)."""
    db = _get_db()
    try:
        from services.gpu_temperature_alerts import evaluate as _temp_eval
        result = _temp_eval(db)
        if result.get("breached"):
            logger.warning("fx136_gpu_temperature_alert_sweep tick: %s "
                           "worker(s) thermally breached (%s)",
                           result["breached"], result["signals"])
        else:
            logger.info("fx136_gpu_temperature_alert_sweep tick: every "
                        "worker within thermal thresholds (%s)",
                        result["signals"])
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx136_gpu_temperature_alert_sweep tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.159 p3b (owner Q146=A) — the CONTRACT-OBLIGATION DUE-DATE REMINDER
# tick. The tracker's reminder sweep lives in services/contract_obligations.py
# (reminder_sweep): due-soon + overdue pushes through the FX.156 alert-rules
# engine (services/security_alert_engine._upsert_alert, so the alert board /
# ack / digest see contract breaches) plus the obligation's owner and the
# tenant's (or platform's) admins through the in-app + email notification
# seam. The tick never raises into APScheduler (that is exactly the "stops
# firing jobs" symptom start_scheduler_watchdog recovers from), and the sweep
# is idempotent: the audit trail is the exactly-once record, so a second pass
# over the same obligation/due-date/kind writes nothing.
# ---------------------------------------------------------------------------


def _fx159_contract_obligation_reminders_tick():
    """One scheduled contract-obligation reminder pass (job id
    fx159_contract_obligation_reminders)."""
    db = _get_db()
    try:
        from services.contract_obligations import reminder_sweep as _co_sweep
        result = _co_sweep(db)
        if result.get("due_soon") or result.get("overdue"):
            logger.info("fx159_contract_obligation_reminders tick: %s", result)
        else:
            logger.info("fx159_contract_obligation_reminders tick: nothing due "
                        "(%s)", result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx159_contract_obligation_reminders tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# GSEC FX.145 p2b (owner decision) — the YEARLY DEVICE-SELF-CHECK sweep.
# Reads the full asset inventory (workers, managers, stacks) from
# services/asset_inventory.py, checks the append-only audit trail for
# existing per-device self-check rows in the current calendar year
# (dedup in Python, no JSONB), and writes one audit row per unchecked
# device. Exactly-once per year per device.
# ---------------------------------------------------------------------------


def _fx145_device_self_check_tick():
    """One scheduled device-self-check pass (job id fx145_device_self_check)."""
    db = _get_db()
    try:
        from services.device_self_check import run as _self_check_run
        result = _self_check_run(db)
        if result.get("new_checks") or result.get("errors"):
            logger.info("fx145_device_self_check tick: %s", result)
        else:
            logger.info("fx145_device_self_check tick: nothing new "
                        "(%s)", result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx145_device_self_check tick raised: %s", e)
    finally:
        db.close()
    return None


# ---------------------------------------------------------------------------
# FX.62 p3a2 — the rolling worker-upgrade campaign tick (job id
# fx62_rolling_upgrade). services/worker_update_rolling.run_upgrade_tick()
# already swallows its own failures and returns a dict, so this wrapper is
# belt-and-braces: a raise from the session plumbing must still never reach
# the scheduler.
# ---------------------------------------------------------------------------


def _fx62_rolling_upgrade_tick():
    """One scheduled rolling-upgrade pass (job id fx62_rolling_upgrade)."""
    db = _get_db()
    try:
        from services.worker_update_rolling import run_upgrade_tick
        result = run_upgrade_tick(lambda: db)
        logger.info("fx62_rolling_upgrade tick: %s", result)
    except Exception as e:  # noqa: BLE001 — never fatal into the scheduler
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("fx62_rolling_upgrade tick raised: %s", e)
    finally:
        db.close()
    return None
